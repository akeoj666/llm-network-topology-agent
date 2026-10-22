"""命令行入口（自然语言通道）：一句话 → 拓扑 DSL → 拓扑图 + .pkt + 配置清单。

用法：
    python nlgen.py "三台路由器两台PC……"
    run-nl "……"
    python nlgen.py "……" --dry-run        # 只出 YAML，不生成 pkt
    python nlgen.py -f prompt.txt          # 需求写在文件里
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from pktlab import audit, cmdguard, nl, pipeline


def next_id(out: Path) -> str:
    """挑一个没被占用的编号。

    模型对编号几乎不敏感——连续两次不同的需求都返回了 UC-91，直接用它的 id
    会把上一批产物覆盖掉。所以编号由我们统一分配：从 91 起找空位。
    """
    used: set[int] = set()
    if out.exists():
        for p in out.glob("UC-*.yaml"):
            m = re.fullmatch(r"UC-(\d+)\.yaml", p.name)
            if m:
                used.add(int(m.group(1)))
    n = max(used | {90}) + 1
    return f"UC-{n:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一句话 → 拓扑图 + .pkt")
    ap.add_argument("prompt", nargs="?", help="中文需求")
    ap.add_argument("-f", "--file", help="从文件读取需求")
    ap.add_argument("-o", "--out", default="out", help="输出目录")
    ap.add_argument("--max-rounds", type=int, default=3, help="校验失败重试上限")
    ap.add_argument("--dry-run", action="store_true", help="只生成 YAML，不生成 pkt")
    ap.add_argument("--model", default=nl.MODEL)
    ap.add_argument("--key", help="直接给 key（默认读 .env / 环境变量）")
    ap.add_argument("--id", help="指定用例编号（默认自动取下一个空闲编号，避免覆盖）")
    ap.add_argument("--no-repair", action="store_true", help="关掉本地确定性修复，所有错误都回灌给模型")
    ap.add_argument(
        "--confirm",
        metavar="CONFIRM",
        help="非交互确认：值必须原样是 CONFIRM（脚本/批量用；交互场景不要传，直接手敲）",
    )
    args = ap.parse_args(argv)

    if args.confirm is not None and args.confirm != cmdguard.CONFIRM_WORD:
        print(f"--confirm 的值必须是 {cmdguard.CONFIRM_WORD}（当前是 {args.confirm!r}）")
        return 2
    confirmer = (
        (lambda gate, cfgs: cmdguard.cli_confirm(gate, cfgs)) if args.confirm else None
    )

    prompt = args.prompt
    if args.file:
        prompt = Path(args.file).read_text(encoding="utf-8").strip()
    if not prompt:
        ap.error("需要一句话需求，或用 -f 指定文件")

    try:
        key = nl.load_api_key(args.key)
    except nl.NLGenError as exc:
        print(exc)
        return 2

    client = nl.Client(key=key, model=args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    uid = args.id or next_id(out)
    if not args.id:
        print(f"未指定 --id，自动分配 {uid}")

    log = audit.Log()
    t0 = time.time()
    res = nl.translate(
        prompt, client,
        max_rounds=args.max_rounds,
        id_hint=uid,
        use_repair=not args.no_repair,
        log=log,
    )
    elapsed = time.time() - t0

    if res.topology:
        uid = res.topology.meta.get("id", uid)

    if res.yaml_text:
        (out / f"{uid}.yaml").write_text(res.yaml_text, encoding="utf-8")

    summary = {
        "轮数": len(res.rounds),
        "总耗时": f"{elapsed:.1f}s",
        "本地修复": f"{res.fixed_total} 处",
        "护栏提示": f"{len([g for g in res.guards if g.level == 'warn'])} 条",
        "结果": "通过" if res.topology else "未通过",
    }

    if res.topology is None:
        # 没通过也要留档：拦截记录本身就是证据
        log.write_jsonl(out / f"{uid}_audit.jsonl")
        (out / f"{uid}_nlog.md").write_text(log.to_markdown(summary), encoding="utf-8")
        print(f"\n{len(res.rounds)} 轮仍未通过。")
        if res.yaml_text:
            print(f"最后一版 DSL 已存到 {out / (uid + '.yaml')}，可手工修正后走 gen.py。")
        print(f"审计：{out / (uid + '_nlog.md')}")
        return 3

    print(f"\n校验通过，用时 {elapsed:.1f}s"
          + (f"，本地修复 {res.fixed_total} 处" if res.fixed_total else "")
          + f"。DSL：{out / (uid + '.yaml')}")
    if args.dry_run:
        log.write_jsonl(out / f"{uid}_audit.jsonl")
        (out / f"{uid}_nlog.md").write_text(log.to_markdown(summary), encoding="utf-8")
        return 0

    try:
        p = pipeline.run(res.topology, out, confirmer=confirmer, log=log)
    except cmdguard.CmdBlocked as exc:
        summary["结果"] = "命令级拦截"
        log.add("fail", stage="cmdscreen", reason=str(exc))
        log.write_jsonl(out / f"{uid}_audit.jsonl")
        (out / f"{uid}_nlog.md").write_text(log.to_markdown(summary), encoding="utf-8")
        print("\n[拦截] 命令级护栏拒绝了这份配置，未生成 pkt：")
        print(exc)
        print(f"审计：{out / (uid + '_audit.jsonl')}")
        return 4
    except cmdguard.ConfirmRefused as exc:
        summary["结果"] = "未确认"
        log.add("fail", stage="confirm", reason=str(exc))
        log.write_jsonl(out / f"{uid}_audit.jsonl")
        (out / f"{uid}_nlog.md").write_text(log.to_markdown(summary), encoding="utf-8")
        print(f"\n[中止] {exc}")
        print("未生成任何 .pkt 文件。")
        return 5

    # 审计最后一步：等 pkt 造出来、验收跑完，才有「下发内容」和「验证结果」可记
    pipeline.finalize_audit(log, res.topology, p, out, summary=summary)
    print(f"产物：{p.pkt}｜{p.png}｜{p.configs}")
    print(f"审计：{out / (uid + '_audit.jsonl')}｜{out / (uid + '_nlog.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
