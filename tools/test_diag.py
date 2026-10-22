"""C3 配置级闭环的测试：5 个坏配置样本 + 一组「不能误报」的反向测试。

判定语是「预置 1 个故障，Agent 在 ≤3 轮内自动定位并修复」，
所以这里要证明三件事：
    ① 该修的修得掉（漏 network / 错掩码 / VLAN 未建）
    ② 修法不唯一的**不瞎改**（ACL 未定义、地址写成网段号 → 只定位）
    ③ 好配置**不误报**（9 个用例的生成结果跑一遍，必须 0 问题）

    python tools/test_diag.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import cmdguard, config, diag  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

FAILS: list[str] = []
CASES = ROOT / "bad_configs"


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}"
          f"{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


def load_case(name: str) -> dict[str, list[str]]:
    d = CASES / name
    return {f.stem: f.read_text(encoding="utf-8").splitlines() for f in sorted(d.glob("*.cfg"))}


def run(name: str) -> diag.Result:
    return diag.diagnose_and_fix(load_case(name))


def codes(issues: list[diag.Issue]) -> set[str]:
    return {i.code for i in issues}


def main() -> int:
    print("[1] 三个「该修」的样本：定位 + 修复 + 复检，轮数 ≤3")

    r1 = run("case1_漏network")
    check("漏 network 被定位", "RIP_MISSING_NET" in codes(r1.issues_before))
    check("1 轮内修完", r1.rounds <= 3, f"{r1.rounds} 轮")
    check("复检后没有可修问题", not [i for i in r1.issues_after if i.fixable])
    check("修复结果里真的补上了 network 10.0.0.0",
          any("network 10.0.0.0" == ln.strip() for ln in r1.configs["R1"]),
          " / ".join(ln.strip() for ln in r1.configs["R1"] if "network" in ln))

    r2 = run("case2_错掩码")
    check("错掩码被定位", "MASK_MISMATCH" in codes(r2.issues_before))
    check("1 轮内修完", r2.rounds <= 3, f"{r2.rounds} 轮")
    check("掩码改成 255.255.255.252",
          any(ln.strip() == "ip address 10.0.0.1 255.255.255.252" for ln in r2.configs["R1"]),
          str([ln for ln in r2.configs["R1"] if "10.0.0.1" in ln]))
    check("另一端（本来就是对的）没被顺手改坏",
          any(ln.strip() == "ip address 10.0.0.2 255.255.255.252" for ln in r2.configs["R2"]),
          str([ln for ln in r2.configs["R2"] if "10.0.0.2" in ln]))

    r3 = run("case3_VLAN未建")
    check("VLAN 未建被定位", "VLAN_UNDEFINED" in codes(r3.issues_before))
    check("1 轮内修完", r3.rounds <= 3, f"{r3.rounds} 轮")
    check("补上了 vlan 20", any(ln.strip() == "vlan 20" for ln in r3.configs["SW0"]))
    check("VLAN 补在 interface 之前（IOS 的位置）",
          r3.configs["SW0"].index("vlan 20") <
          next(i for i, ln in enumerate(r3.configs["SW0"]) if ln.startswith("interface ")),
          str(r3.configs["SW0"][:20]))
    check("没有编造 VLAN 名字（名字不在坏配置里）",
          not [ln for ln in r3.configs["SW0"] if ln.strip() == "name VLAN020"])

    print("\n[2] 修完应该还原成「原样的好配置」（比只看关键词硬）")
    for case, uid, dev in (("case1_漏network", "uc01", "R1"),
                           ("case2_错掩码", "uc01", "R1")):
        good = config.generate_all(Topology.load(ROOT / "examples" / f"{uid}.yaml"))[dev]
        got = run(case).configs[dev]
        check(f"{case} 的 {dev} 与原配置逐行相同", got == good,
              str([x for x in got if x not in good][:3]))

    print("\n[3] 两个「改法不唯一」的样本：只定位，不瞎改")

    r4 = run("case4_ACL未定义")
    check("ACL 未定义被定位", "ACL_UNKNOWN" in codes(r4.issues_before))
    check("没有自动改（改动 0 处）", not r4.fixes, str(r4.fixes))
    check("原文一字未动",
          r4.configs["R1"] == load_case("case4_ACL未定义")["R1"])

    r5 = run("case5_地址非法")
    check("地址写成网段号被定位", "BAD_ADDR" in codes(r5.issues_before))
    check("没有自动改", not r5.fixes, str(r5.fixes))

    print("\n[4] 不能误报：9 个用例的正常配置跑一遍，必须 0 问题")
    good_files = sorted(p.name for p in (ROOT / "examples").glob("uc*.yaml")
                        if p.name != "uc12_bad.yaml")
    bad_report: list[str] = []
    for f in good_files:
        topo = Topology.load(ROOT / "examples" / f)
        issues = diag.check(diag.parse(config.generate_all(topo)))
        if issues:
            bad_report.append(f"{f}: " + "; ".join(str(i) for i in issues))
    check(f"{len(good_files)} 个用例的配置全部零误报", not bad_report,
          " | ".join(bad_report[:2]))

    print("\n[5] 修完的配置必须能过命令级白名单（否则「修好」是假的）")
    for name in ("case1_漏network", "case2_错掩码", "case3_VLAN未建"):
        gate = cmdguard.check_all(run(name).configs)
        check(f"{name} 修后白名单通过", gate.passed,
              f"拦截 {len(gate.blocked)} 条")

    print("\n[6] 幂等：把修好的再喂一遍，不该再产生改动")
    for name in ("case1_漏network", "case2_错掩码", "case3_VLAN未建"):
        again = diag.diagnose_and_fix(run(name).configs)
        check(f"{name} 二次诊断 0 改动", not again.fixes, str(again.fixes))

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
