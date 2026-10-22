"""编址规划书的测试：内容对不对、有没有把该有的信息漏掉。

要守住的四件事：
    ① 规划图里**每台设备都出现**，且地址与 DSL 一致
    ② IP 编址表**逐接口**都有，且掩码/网段算得对
    ③ VLAN 表覆盖所有被引用的 VLAN
    ④ 层次判断符合直觉：默认路由那台是出口、它的下一跳那台是 ISP

    python tools/test_plan.py
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import plan as plan_mod  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}"
          f"{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    files = sorted((ROOT / "examples").glob("uc*.yaml"))
    files = [f for f in files if f.name != "uc12_bad.yaml"]
    files += sorted((ROOT / "out").glob("UC-*.yaml"))

    print(f"[1] {len(files)} 个拓扑都要能生成规划书，且设备一个不落")
    for f in files:
        try:
            topo = Topology.load(f)
            p = plan_mod.build(topo)
        except Exception as exc:
            check(f"{f.name} 生成规划书", False, str(exc))
            continue
        missing = [d["name"] for d in topo.data["devices"] if d["name"] not in p.ascii]
        check(f"{f.name}：规划图里设备齐全（{len(topo.data['devices'])} 台）",
              not missing, "缺 " + "、".join(missing))

    print("\n[2] IP 编址表：逐接口都有，掩码与网段算得对")
    topo = Topology.load(ROOT / "examples" / "uc01.yaml")
    p = plan_mod.build(topo)
    want_ifaces = sum(len(d.get("interfaces") or []) for d in topo.data["devices"]
                       if d.get("role") not in ("pc", "server"))
    got_ifaces = len([r for r in p.addrs if r.device and r.port != "(网卡)"])
    check("接口条数与 DSL 一致", want_ifaces == got_ifaces, f"{want_ifaces} vs {got_ifaces}")
    bad = []
    for r in p.addrs:
        if not r.ip:
            continue
        try:
            iface = ipaddress.ip_interface(f"{r.ip}/{r.mask}")
        except ValueError:
            bad.append(f"{r.device} {r.port} {r.ip}/{r.mask}")
            continue
        if str(iface.network) != r.network:
            bad.append(f"{r.device} {r.port} 网段算错 {r.network} != {iface.network}")
    check("每行的掩码与网段自洽", not bad, " | ".join(bad[:3]))
    r1 = next(r for r in p.addrs if r.device == "R1" and r.port == "FastEthernet2/0")
    check("业务网关口被标成「业务网关」", r1.usage == "业务网关", r1.usage)
    r1s = next(r for r in p.addrs if r.device == "R1" and r.port == "Serial0/0")
    check("点到点互联口被标成「互联」", r1s.usage == "互联", r1s.usage)
    shut = next(r for r in p.addrs if r.device == "R1" and r.port == "FastEthernet3/0")
    check("shutdown 的口标成「备用」并注明", shut.usage == "备用" and "shutdown" in shut.note,
          f"{shut.usage} {shut.note}")

    print("\n[3] VLAN 表：被引用的 VLAN 都要在")
    topo7 = Topology.load(ROOT / "examples" / "uc07.yaml")
    p7 = plan_mod.build(topo7)
    vids = {v["id"] for v in topo7.data.get("vlans") or []}
    got = {v.vid for v in p7.vlans}
    check("UC-07 的 VLAN 30/40 都在表里", vids <= got, f"{sorted(vids)} vs {sorted(got)}")
    v30 = next((v for v in p7.vlans if v.vid == 30), None)
    check("VLAN 30 的网关取到 192.168.30.254",
          v30 is not None and v30.gateway == "192.168.30.254",
          v30.gateway if v30 else "缺")

    print("\n[4] 层次判断")
    check("UC-07：R1（有默认路由）是边界/出口",
          any(d.name == "R1" and d.layer == "边界/出口" for d in p7.devices),
          str({d.name: d.layer for d in p7.devices}))
    check("UC-07：R2（默认路由的下一跳）是 ISP/上游",
          any(d.name == "R2" and d.layer == "ISP/上游" for d in p7.devices),
          str({d.name: d.layer for d in p7.devices}))
    check("UC-07：规划图把 R2 画在最上游",
          p7.ascii.index("R2") < p7.ascii.index("R1"), "顺序不对")
    check("UC-07：图上有「上游网络 / Internet」", "上游网络 / Internet" in p7.ascii)
    p1 = plan_mod.build(Topology.load(ROOT / "examples" / "uc01.yaml"))
    check("UC-01：没有默认路由就不画上游",
          "上游网络" not in p1.ascii and all(d.layer != "ISP/上游" for d in p1.devices))

    print("\n[5] 每段链路都要标出网段")
    for name, pl in (("UC-01", p1), ("UC-07", p7)):
        no_net = [ln.strip() for ln in pl.ascii.splitlines() if "↔" in ln and "/" not in ln]
        check(f"{name}：所有连线都带网段", not no_net, " | ".join(no_net[:2]))

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
