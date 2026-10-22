"""命令行入口（YAML 通道）：DSL 文件 → 拓扑图 + .pkt + 配置清单。

用法：
    python gen.py examples/uc01.yaml
    run uc05
    python gen.py examples/uc01.yaml --confirm CONFIRM     # 非交互（脚本/批量）

关于确认门：默认在终端里打印命令级筛查摘要并要求敲大写 CONFIRM；
没敲就不生成 pkt。`--confirm CONFIRM` 是给批量/自动化用的等价显式确认，
审计日志里会如实记为「命令行参数 --confirm」，不会伪装成人工交互。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pktlab import audit, cmdguard, pipeline
from pktlab.dsl import Topology


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DSL → 拓扑图 + .pkt")
    ap.add_argument("dsl", help="DSL YAML 文件")
    ap.add_argument("-o", "--out", default="out", help="输出目录")
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

    topo = Topology.load(args.dsl)
    topo.validate()
    if not topo.ok:
        print("校验未通过，拒绝生成：")
        print(topo.report())
        return 2
    if topo.issues:
        print(topo.report())

    # YAML 通道也留一份审计日志：没有模型参与，但「下发内容 / 验证结果 / 校验问题 /
    # 命令级筛查 / 确认来源」一样要有档，否则复现时说不清 pkt 里到底写了什么。
    log = audit.Log(prompt=str(topo.meta.get("source_prompt", "")), model="（YAML 通道，无模型参与）")
    if topo.issues:
        log.add("validate", round=0, issues=[str(i) for i in topo.issues])
    gs = audit.guard(topo, str(topo.meta.get("source_prompt", "")))
    if gs:
        log.add("guard", items=[str(g) for g in gs], blocked=len(audit.blocked(gs)))

    try:
        p = pipeline.run(topo, args.out, confirmer=confirmer, log=log)
    except cmdguard.CmdBlocked as exc:
        uid = topo.meta.get("id", "topology")
        log.add("fail", stage="cmdscreen", reason=str(exc))
        log.write_jsonl(Path(args.out) / f"{uid}_audit.jsonl")
        print("\n[拦截] 命令级护栏拒绝了这份配置，未生成 pkt：")
        print(exc)
        print(f"\n审计：{Path(args.out) / (uid + '_audit.jsonl')}")
        return 4
    except cmdguard.ConfirmRefused as exc:
        print(f"\n[中止] {exc}")
        print("未生成任何 .pkt 文件。")
        return 5

    pipeline.finalize_audit(log, topo, p, args.out, summary={"通道": "YAML 直读", "结果": "通过"})
    print(f"产物：{p.pkt}｜{p.png}｜{p.configs}")
    print(f"审计：{p.pkt.parent / (topo.meta.get('id', 'topology') + '_audit.jsonl')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
