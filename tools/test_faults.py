"""C3 故障注入闭环：注入一类错，看它能不能被「报对 → 修好 → 收敛」。

三段式。
  ① 定位：校验器必须报出预期的那条错误码。报不出 ⇒ 这条错误对模型不可回灌。
  ② 闭环：修复器必须把它修到校验通过，且**再修一次不再有改动**（幂等）。
  ③ 语义：修完之后，C2 的验收断言和一致性检查仍然全过。

第 ③ 段是关键。只做到 ①② 很容易：把出错字段删掉就行，
校验当然能过，但拓扑已经不是用户要的那个了。所以必须有 ③ 拦着。

有几条故障故意标了 strict=False —— 它们的原意无法从残骸里推断出来，
修完能编译但意思变了。宁可把它标出来，也不假装修好了。

    python tools/test_faults.py            # 跑全部
    python tools/test_faults.py -k acl     # 只跑 key 里含 acl 的
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import accept, faults, repair  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402


def codes(topo: Topology) -> set[str]:
    return {i.code for i in topo.issues if i.level == "error"}


def run_one(f: faults.Fault, verbose: bool) -> tuple[bool, list[str]]:
    notes: list[str] = []
    fails: list[str] = []

    # 基线本身必须是干净的，否则后面全是噪音
    base = faults.load(f.base)
    bt = Topology(base)
    bt.validate()
    if codes(bt):
        return False, [f"基线 {f.base} 自己就有错：{sorted(codes(bt))}"]

    data = f.inject()
    topo = Topology(data)
    topo.validate()
    got = codes(topo)

    # ① 报对码
    if f.code not in got:
        fails.append(f"① 没报出 {f.code}（报的是 {sorted(got) or '无'}）")

    res = repair.repair(data)
    left = {i.code for i in res.remaining}

    # ② 收敛 + 幂等
    if f.fixable:
        if res.remaining:
            fails.append(f"② 修完还有错：{sorted(left)}")
            for i in res.remaining[:3]:
                fails.append(f"    {i}")
        again = repair.repair(res.data)
        if again.fixed:
            fails.append(f"② 不幂等：第二次又改了 {len(again.fixed)} 处")
    else:
        if f.code not in left:
            fails.append(f"② 本该修不掉的 {f.code} 居然被「修」没了，检查修复器是否在瞎猜")

    # ③ 语义
    if f.fixable and f.strict and not res.remaining:
        rt = Topology(res.data)
        rt.validate()
        rep = accept.analyze(rt)
        if rep.failed_asserts:
            for a in rep.failed_asserts[:3]:
                fails.append(f"③ 断言 {a.src}→{a.dst} 期望{a.want}实际{a.got}")
        if rep.failed_checks:
            for m in rep.failed_checks[:3]:
                fails.append(f"③ 检查未过：{m}")

    if verbose:
        tag = "可修" if f.fixable else "不修"
        tag += "" if f.strict else "/语义待确认"
        notes.append(f"  注入 → {sorted(got)}")
        for fx in res.fixed:
            notes.append(f"    · {fx}")
        notes.append(f"  剩余 → {sorted(left) or '无'}｜修复 {len(res.fixed)} 处 / {res.passes} 轮 [{tag}]")

    return not fails, fails + notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C3 故障注入闭环测试")
    ap.add_argument("-k", "--key", help="只跑 key 里含该子串的故障")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每处修复明细")
    a = ap.parse_args(argv)

    catalog = faults.all_faults()
    if a.key:
        catalog = [f for f in catalog if a.key in f.key]

    print(f"C3 故障注入闭环：{len(catalog)} 条故障\n")

    bad: list[str] = []
    n_fix = n_strict = n_keep = 0
    for f in catalog:
        ok, msgs = run_one(f, a.verbose)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {f.key:<22} {f.desc}")
        if a.verbose:
            for m in msgs:
                print(m)
        if not ok:
            bad.append(f.key)
            for m in msgs:
                if not m.startswith(" "):
                    print(f"         {m}")
                else:
                    print(f"       {m}")
        elif f.fixable:
            n_fix += 1
            if f.strict:
                n_strict += 1
        else:
            n_keep += 1      # 按设计不修的

    print("")
    print(f"共 {len(catalog)} 条故障：")
    print(f"  · 可修 {n_fix} 条 —— 全部闭环收敛，其中 {n_strict} 条同时保住了语义")
    print(f"  · 不修 {n_keep} 条 —— 正确报告出来、没有瞎改（怎么挪没有唯一答案）")
    if bad:
        print(f"未通过：{', '.join(bad)}")
        return 1
    print("全部符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
