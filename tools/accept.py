"""验收命令行：给一批用例，输出验收报告 + 通过/失败结论。

  python tools/accept.py --all
  python tools/accept.py UC-07 UC-05
  python tools/accept.py examples/uc01.yaml -o out

注意：这是静态推算 + 一致性检查，不启动 Packet Tracer。
报告里会写清楚「哪些是算的、哪些要人去 PT 里点」。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pktlab import accept  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

OUT = ROOT / "out"
EXAMPLES = ROOT / "examples"


# ---------------------------------------------------------------- 用例定位
def _key(arg: str) -> str:
    """UC-07 / uc-07 / uc07 → uc07"""
    return arg.replace("-", "").replace("_", "").lower()


def resolve(arg: str) -> Path | None:
    p = Path(arg)
    if p.is_file() and p.suffix.lower() in (".yaml", ".yml"):
        return p
    k = _key(arg)
    for cand in (OUT / f"{arg}.yaml", OUT / f"{arg.upper()}.yaml",
                 EXAMPLES / f"{arg}.yaml", EXAMPLES / f"{k}.yaml"):
        if cand.is_file():
            return cand
    return None


def resolve_all() -> list[Path]:
    """out 优先（可能是模型新生成的），examples 兜底。"""
    seen: dict[str, Path] = {}
    for folder in (EXAMPLES, OUT):
        for p in sorted(folder.glob("*.yaml")):
            seen[_key(p.stem)] = p
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------- 跑一个
@dataclass
class CaseResult:
    uid: str
    title: str
    ok: bool
    note: str
    pairs: int = 0
    reach: int = 0
    block: int = 0
    asserts: int = 0
    asserts_ok: int = 0
    checks: int = 0
    checks_fail: int = 0
    traces: int = 0
    traces_ok: int = 0
    routes: int = 0
    routes_ok: int = 0
    has_pkt: bool = False


def run_one(path: Path, out_dir: Path, write: bool) -> CaseResult:
    topo = Topology.load(path)
    issues = topo.validate()
    errors = [i for i in issues if i.level == "error"]
    uid = topo.meta.get("id", path.stem)
    title = topo.meta.get("title", "")
    reject_case = "bad" in path.stem.lower()
    has_pkt = (OUT / f"{uid}.pkt").is_file() or (out_dir / f"{uid}.pkt").is_file()

    if errors:
        if reject_case:
            # 这类用例本来就是「应该被拒绝」的陷阱题，报错即达标
            return CaseResult(uid, title, True, f"按预期被拒绝（{len(errors)} 条错误）", has_pkt=has_pkt)
        print(f"[{uid}] 校验没过，跳过验收（{len(errors)} 条错误）")
        for i in errors[:5]:
            print(f"    {i}")
        return CaseResult(uid, title, False, f"校验未通过（{len(errors)} 条错误）", has_pkt=has_pkt)

    if reject_case:
        return CaseResult(uid, title, False, "本该被拒绝，却通过了校验", has_pkt=has_pkt)

    rep = accept.analyze(topo)
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{uid}_验收报告.md").write_text(
            accept.to_markdown(rep, topo), encoding="utf-8"
        )

    ok = rep.passed and not rep.failed_checks
    note = f"断言 {len(rep.asserts) - len(rep.failed_asserts)}/{len(rep.asserts)}" if rep.asserts else "无断言"
    if rep.failed_checks:
        note += f"，检查未过 {len(rep.failed_checks)} 项"
    return CaseResult(
        uid, title, ok, note,
        pairs=len(rep.pairs), reach=rep.n_reach, block=rep.n_block,
        asserts=len(rep.asserts), asserts_ok=len(rep.asserts) - len(rep.failed_asserts),
        checks=len(rep.checks), checks_fail=len(rep.failed_checks),
        traces=len(rep.traces), traces_ok=sum(1 for t in rep.traces if t.ok),
        routes=len(rep.routes), routes_ok=sum(1 for r in rep.routes if r.ok),
        has_pkt=has_pkt,
    )


def write_summary(results: list[CaseResult], out_dir: Path) -> Path:
    """给最终报告的「量化评测」表提供数据。"""
    n = len(results)
    ok = sum(1 for r in results if r.ok)
    tot_a = sum(r.asserts for r in results)
    ok_a = sum(r.asserts_ok for r in results)
    tot_c = sum(r.checks for r in results)
    f_c = sum(r.checks_fail for r in results)
    tot_pairs = sum(r.pairs for r in results)
    tot_t = sum(r.traces for r in results)
    tot_r = sum(r.routes for r in results)
    ok_t = sum(r.traces_ok for r in results)
    ok_r = sum(r.routes_ok for r in results)

    lines = [
        "# 验收总表",
        "",
        f"- 用例 {n} 个，通过 {ok} 个",
        f"- 断言 {ok_a}/{tot_a} 通过（{ok_a / tot_a * 100:.1f}%）" if tot_a else "- 无断言",
        f"- 一致性检查 {tot_c - f_c}/{tot_c} 通过" if tot_c else "- 无检查",
        f"- 推算主机对 {tot_pairs} 组",
        f"- traceroute 路径断言 {ok_t}/{tot_t} 条（预期可通 / 应不通）" if tot_t else "- 无路径断言",
        f"- 路由表断言 {ok_r}/{tot_r} 条（预期成立 / 预期缺失）" if tot_r else "- 无路由断言",
        "",
        "| 用例 | 场景 | pkt | 主机对 | 通/不通 | 断言 | 路径 | 路由 | 检查 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.uid} | {r.title} | {'有' if r.has_pkt else '—'} | {r.pairs} | "
            f"{r.reach}/{r.block} | {r.asserts_ok}/{r.asserts} | "
            f"{r.traces_ok}/{r.traces} | {r.routes_ok}/{r.routes} | "
            f"{r.checks - r.checks_fail}/{r.checks} | {'PASS' if r.ok else '**FAIL**'} |"
        )
    lines += [
        "",
        "> 口径说明：主机对 / 通 / 不通 是**推算值**（同网段二层直通、跨网段按 IGP 已收敛找路、",
        "> ACL 按序首条命中），不是 PT 实跑结果。断言通过率反映「生成结果与需求意图是否一致」，",
        "> 路径 / 路由两列是「预期值」的条数（应不通的那些本来就不该通，所以路径列不是 100%），",
        "> 一致性检查通过率反映「配置是否自洽」。都不等价于「PT 里真的 ping 通了」。",
        "",
    ]
    p = out_dir / "验收总表.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="拓扑验收（静态推算，不启动 PT）")
    ap.add_argument("cases", nargs="*", help="用例名（UC-07）或 YAML 路径")
    ap.add_argument("--all", action="store_true", help="跑全部用例")
    ap.add_argument("-o", "--out", default=str(OUT), help="报告输出目录")
    ap.add_argument("--no-write", action="store_true", help="只看结论，不落文件")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    out_dir = Path(a.out)
    if a.all:
        paths = resolve_all()
    else:
        paths = []
        for c in a.cases:
            p = resolve(c)
            if p is None:
                print(f"找不到用例：{c}")
            else:
                paths.append(p)

    if not paths:
        print("没有可跑的用例。用法：python tools/accept.py --all")
        return 2

    print(f"验收 {len(paths)} 个用例（静态推算，不启动 Packet Tracer）")
    print("")

    results: list[CaseResult] = []
    bad: list[str] = []
    for p in paths:
        r = run_one(p, out_dir, not a.no_write)
        results.append(r)
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.uid:<8} {r.note}")
        if not r.ok:
            bad.append(r.uid)

    summary = None
    if not a.no_write:
        summary = write_summary(results, out_dir)

    print("")
    if summary:
        print(f"总表：{summary}")
    if bad:
        print(f"{len(bad)} 个用例未通过：{', '.join(bad)}")
        print("细节见 out/ 下对应的 *_验收报告.md")
        return 1
    print(f"全部 {len(paths)} 个用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
