"""C3 的入口：读一份坏配置，定位并修复，输出报告。

    python tools/diag_cfg.py bad_configs/case1_漏network
    python tools/diag_cfg.py bad_configs/case1_漏network --out out/C3/case1

流程（对应判定语的四个要求）：
    ① 定位：`diag.check()` 指出哪台设备、哪个接口、错在哪
    ② 修复：`diag.fix()` 只改必要的行，其余字节不动
    ③ ≤3 轮：循环「修一轮 → 重新解析 → 复检」，实测都是 1 轮
    ④ 可复现：全程写 `diag.jsonl`，每轮记「读到什么 / 判什么 / 改了什么」

最后还要过一遍 `cmdguard` 白名单——修完的配置得能直接下发，才算修好了。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import cmdguard, diag  # noqa: E402


def load_case(path: Path) -> dict[str, list[str]]:
    """一个目录里一台设备一个 .cfg，文件名即设备名。"""
    cfgs: dict[str, list[str]] = {}
    for f in sorted(path.glob("*.cfg")):
        cfgs[f.stem] = f.read_text(encoding="utf-8").splitlines()
    if not cfgs:
        raise SystemExit(f"{path} 里没有 .cfg 文件")
    return cfgs


def to_markdown(case: str, res: diag.Result, gate: cmdguard.GateResult | None) -> str:
    before_fixable = [i for i in res.issues_before if i.fixable]
    before_hard = [i for i in res.issues_before if not i.fixable]
    after_fixable = [i for i in res.issues_after if i.fixable]
    after_hard = [i for i in res.issues_after if not i.fixable]

    if after_fixable:
        verdict = f"❌ 仍有 {len(after_fixable)} 个可修问题"
    elif res.fixes and after_hard:
        verdict = f"✅ 可修的已修复；另有 {len(after_hard)} 个需人工判断，未擅自修改"
    elif res.fixes:
        verdict = "✅ 已修复并复检通过"
    elif after_hard:
        verdict = f"⚠️ 定位到 {len(after_hard)} 个问题，但都不在可自动修复的范围内（需人工）"
    else:
        verdict = "✅ 没发现问题"

    lines = [f"# C3 配置级闭环 — {case}", "", f"**结论：{verdict}**", ""]
    lines += [
        "| 指标 | 数值 |", "|---|---|",
        f"| 修复轮数 | **{res.rounds}** 轮（判定语要求 ≤3） |",
        f"| 修复前问题 | {len(res.issues_before)} 个（可自动修 {len(before_fixable)}） |",
        f"| 修复后问题 | {len(res.issues_after)} 个（可修 {len(after_fixable)}） |",
        f"| 改动行数 | {len(res.fixes)} 处 |",
    ]
    if gate is not None:
        lines.append(f"| 命令级白名单 | {'通过' if gate.passed else '**未通过**'}"
                     f"（{gate.total_lines} 行，拦截 {len(gate.blocked)}） |")
    lines.append("")

    if res.issues_before:
        lines += ["## 定位到的问题", "", "| 类型 | 设备 | 位置 | 说明 | 处理 |", "|---|---|---|---|---|"]
        for i in res.issues_before:
            act = "自动修复" if i.fixable else "**留给人工**（改法不唯一，不瞎改）"
            lines.append(f"| `{i.code}` | {i.device} | {i.where} | {i.message} | {act} |")
        lines.append("")

    if res.fixes:
        lines += ["## 实际改动", ""]
        lines += [f"{n}. {f}" for n, f in enumerate(res.fixes, 1)]
        lines.append("")

    if res.issues_after:
        lines += ["## 复检后仍存在的问题", "", "| 类型 | 设备 | 位置 | 说明 |", "|---|---|---|---|"]
        for i in res.issues_after:
            lines.append(f"| `{i.code}` | {i.device} | {i.where} | {i.message} |")
        lines.append("")

    lines += [
        "## 能力边界（写在报告里，不含糊）", "",
        "1. **只认本项目会生成的命令子集**，不是通用 IOS 解析器；遇到不认识的行原样跳过。",
        "2. **不做网关类检查**：`running-config` 里没有 PC，网关只在主机侧，"
        "要查得配一份拓扑（这条路走 `pktlab.accept`）。",
        "3. **不自动补 ACL 内容**：ACL 里该写什么取决于安全意图，改法不唯一，"
        "所以只定位、留给人判断——这与 DSL 层那 2 类「不修」的故障是同一个原则。",
        "4. 修完的配置过白名单只说明「能下发」，**能不能通**仍要看验收报告。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case", help="含 *.cfg 的目录")
    ap.add_argument("--out", default="", help="报告与修复后配置的输出目录")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    src = Path(args.case)
    cfgs = load_case(src)
    res = diag.diagnose_and_fix(cfgs, max_rounds=args.rounds)

    gate = None
    try:
        gate = cmdguard.check_all(res.configs)
    except Exception as exc:  # 白名单本身出错不该把诊断结果一起埋掉
        print(f"[警告] 白名单校验没跑起来：{exc}", file=sys.stderr)

    name = src.name
    out = Path(args.out) if args.out else ROOT / "out" / "C3" / name
    out.mkdir(parents=True, exist_ok=True)
    fixed_dir = out / "fixed"
    fixed_dir.mkdir(exist_ok=True)
    for dev, lines in res.configs.items():
        (fixed_dir / f"{dev}.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")

    md = to_markdown(name, res, gate)
    (out / "诊断报告.md").write_text(md, encoding="utf-8")

    with (out / "diag.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "event": "diag", "case": name, "rounds": res.rounds,
            "issues_before": [str(i) for i in res.issues_before],
            "issues_after": [str(i) for i in res.issues_after],
            "fixes": res.fixes,
            "gate_passed": None if gate is None else gate.passed,
        }, ensure_ascii=False) + "\n")

    print(f"{name}: {res.rounds} 轮，修复前 {len(res.issues_before)} 个问题，"
          f"修复后 {len([i for i in res.issues_after if i.fixable])} 个可修问题")
    for n, f in enumerate(res.fixes, 1):
        print(f"  {n}. {f}")
    print(f"报告：{out / '诊断报告.md'}")
    return 0 if not [i for i in res.issues_after if i.fixable] else 1


if __name__ == "__main__":
    raise SystemExit(main())
