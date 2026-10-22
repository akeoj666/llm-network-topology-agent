"""验收器的反向测试：故意把拓扑改坏，确认它真的能抓出来。

一个永远通过的检查器没有价值，所以每个故障都必须被指出，
而且要说清楚「哪台设备、哪个口、哪个 VLAN」。
不联网，不启动 Packet Tracer。

    python tools/test_accept.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import accept  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

FAILS: list[str] = []


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / "examples" / name).read_text(encoding="utf-8"))


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {label}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


def dev(data: dict, name: str) -> dict:
    return next(d for d in data["devices"] if d["name"] == name)


def port(data: dict, name: str, ifname: str) -> dict:
    return next(i for i in dev(data, name)["interfaces"] if i["name"] == ifname)


def run(data: dict) -> accept.Report:
    topo = Topology(data)
    topo.validate()
    return accept.analyze(topo)


def failures(rep: accept.Report) -> str:
    return " | ".join(rep.failed_checks)


# ------------------------------------------------------------------ 用例
def t_baseline() -> None:
    print("\n[1] 基线：好的拓扑必须全过")
    for f in ("uc01.yaml", "uc03.yaml", "uc05.yaml", "uc06.yaml", "uc07.yaml"):
        rep = run(load(f))
        check(f"{f} 断言全过", not rep.failed_asserts, failures(rep))
        check(f"{f} 检查全过", not rep.failed_checks, failures(rep))


def t_wrong_access_vlan() -> None:
    print("\n[2] 故障：PC3 的交换机口划错 VLAN（20 → 10）")
    data = load("uc05.yaml")
    port(data, "SW0", "FastEthernet0/4")["vlan"] = 10
    rep = run(data)
    msg = failures(rep)
    check("抓到了", "PC3" in msg and "VLAN 20" in msg, msg)
    # 三层模型看不到这个错，所以连通性矩阵仍然全通 —— 这正是要单独查二层的原因
    check("三层矩阵察觉不到（符合预期）", rep.n_block == 0, f"不通过 {rep.n_block} 组")


def t_no_trunk() -> None:
    print("\n[3] 故障：SW0 的 Fa0/24 不再是 trunk")
    data = load("uc05.yaml")
    p = port(data, "SW0", "FastEthernet0/24")
    p["mode"] = "access"
    p["vlan"] = 1
    rep = run(data)
    msg = failures(rep)
    check("抓到了", "trunk" in msg, msg)


def t_missing_subif() -> None:
    print("\n[4] 故障：R1 少了 VLAN 20 的子接口")
    data = load("uc05.yaml")
    r1 = dev(data, "R1")
    r1["interfaces"] = [i for i in r1["interfaces"] if i.get("dot1q") != 20]
    rep = run(data)
    msg = failures(rep)
    check("检查项抓到缺子接口", "VLAN 20" in msg and "子接口" in msg, msg)
    check("跨 VLAN 判定变为不通", rep.n_block > 0, f"不通过 {rep.n_block} 组")


def t_acl_direction() -> None:
    print("\n[5] 故障：ACL 挂反方向（in → out）")
    data = load("uc07.yaml")
    data["acls"][0]["apply"][0]["direction"] = "out"
    rep = run(data)
    # 挂到 out 就不再拦「从客人 VLAN 进来」的包了，PC0→PC2（客人→收银）应该变回通
    p = next(x for x in rep.pairs if x.src == "PC0" and x.dst == "PC2")
    check("客人→收银 不再被拦（方向确实起作用）", p.ok, p.reason)
    check("但断言因此失败", bool(rep.failed_asserts), "断言竟然全过了")
    bad = next(a for a in rep.failed_asserts if (a.src, a.dst) == ("PC0", "PC2"))
    check("失败原因写清楚了", "ACL" in bad.reason or "路由" in bad.reason, bad.reason)


def t_no_fallback() -> None:
    print("\n[6] 故障：ACL 去掉 permit 兜底")
    data = load("uc07.yaml")
    data["acls"][0]["rules"] = data["acls"][0]["rules"][:1]  # 只留 deny
    rep = run(data)
    msg = failures(rep)
    check("抓到缺兜底", "permit" in msg, msg)
    # 末尾隐含 deny 会把客人→公网 也一起打掉
    p = next(x for x in rep.pairs if x.src == "PC0" and x.dst == "SRV0")
    check("客人→公网 被隐含 deny 打掉", not p.ok, p.reason)
    check("原因标为隐含 deny", "隐含" in p.reason, p.reason)


def t_gateway_typo() -> None:
    print("\n[7] 故障：主机网关写错")
    data = load("uc01.yaml")
    dev(data, "PC0")["gateway"] = "192.168.16.99"
    rep = run(data)
    msg = failures(rep)
    check("抓到网关无效", "192.168.16.99" in msg, msg)
    p = next(x for x in rep.pairs if x.src == "PC0")
    check("判为不通", not p.ok, p.reason)


def t_no_routing() -> None:
    print("\n[8] 故障：两台路由器都不跑路由协议")
    data = load("uc02.yaml")
    data["routing"] = []
    rep = run(data)
    p = next(x for x in rep.pairs if x.src == "PC0" and x.dst == "PC2")
    check("跨三跳判为不通", not p.ok, p.reason)
    check("原因提到路由", "路由" in p.reason, p.reason)


def t_unknown_device() -> None:
    print("\n[9] 断言写错设备名")
    data = load("uc01.yaml")
    data["expect"] = {"reach": [["PC0", "PC9"]]}
    rep = run(data)
    check("标为未推算", rep.asserts and rep.asserts[0].got == "未推算", str(rep.asserts))
    check("算作不通过", bool(rep.failed_asserts))


def t_trace_basic() -> None:
    print("\n[10] C2：traceroute 跳序（通）")
    data = load("uc01.yaml")
    rep = run(data)
    t = next(x for x in rep.traces if x.src == "PC0" and x.dst == "PC1")
    check("预期可通", t.ok, t.note)
    check("3 跳", len(t.hops) == 3, str(t.hops))
    check("首跳是网关", t.hops[0] == "192.168.16.62", str(t.hops))
    check("末跳是目的主机", t.hops[-1] == "192.168.16.138", str(t.hops))
    check("路径是 PC0→R1→R3→PC1", t.routers == ["R1", "R3"], str(t.routers))


def t_trace_acl() -> None:
    print("\n[11] C2：traceroute 在 ACL 处截断（不通）")
    data = load("uc07.yaml")
    rep = run(data)
    t = next(x for x in rep.traces if x.src == "PC0" and x.dst == "PC2")
    check("预期不通", not t.ok, t.note)
    check("只到网关这一跳", len(t.hops) == 1, str(t.hops))
    check("说明是静默丢弃", "*  *  *" in t.note, t.note)
    check("点明是哪台路由器", "R1" in t.note, t.note)


def t_trace_no_route() -> None:
    print("\n[12] C2：中途没有路由时 traceroute 停在 !H")
    data = load("uc02.yaml")
    dev(data, "R2")["interfaces"] = [
        i for i in dev(data, "R2")["interfaces"] if not i.get("ip", "").startswith("10.0.0.3")
    ] or dev(data, "R2")["interfaces"]
    data["routing"] = [r for r in data["routing"] if r["device"] != "R2"]
    rep = run(data)
    bad = [t for t in rep.traces if not t.ok]
    check("有判为不通的路径", bool(bad), "全部可通，用例没构造成功")
    if bad:
        check("说明里带 !H 或超时", any("!H" in t.note or "超时" in t.note for t in bad),
              str([t.note for t in bad]))


def t_route_table() -> None:
    print("\n[13] C2：路由表断言")
    data = load("uc01.yaml")
    rep = run(data)
    check("每台路由器都有条目", {r.device for r in rep.routes} == {"R1", "R2", "R3"},
          str({r.device for r in rep.routes}))
    r1 = [r for r in rep.routes if r.device == "R1"]
    check("R1 有直连条目", any(r.code == "C" for r in r1), str([r.entry for r in r1]))
    check("R1 有 RIP 条目", any(r.code == "R" for r in r1), str([r.entry for r in r1]))
    rip = next(r for r in r1 if r.code == "R")
    check("RIP 度量是跳数 1", "[120/1]" in rip.entry, rip.entry)
    check("下一跳是该网段的路由器接口", rip.entry.count("via") == 1, rip.entry)
    check("全部预期成立", all(r.ok for r in rep.routes),
          str([(r.device, r.prefix, r.note) for r in rep.routes if not r.ok]))


def t_route_missing() -> None:
    print("\n[14] C2：缺路由时路由断言要标出来")
    data = load("uc01.yaml")
    data["routing"] = [r for r in data["routing"] if r["device"] != "R1"]
    rep = run(data)
    bad = [r for r in rep.routes if not r.ok]
    check("R1 的远端条目被标为不成立", any(r.device == "R1" for r in bad),
          str([(r.device, r.prefix) for r in rep.routes]))


def main() -> int:
    print("验收器反向测试（不联网）")
    for fn in (
        t_baseline, t_wrong_access_vlan, t_no_trunk, t_missing_subif,
        t_acl_direction, t_no_fallback, t_gateway_typo, t_no_routing, t_unknown_device,
        t_trace_basic, t_trace_acl, t_trace_no_route, t_route_table, t_route_missing,
    ):
        fn()
    print("")
    if FAILS:
        print(f"{len(FAILS)} 项没达到预期：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
