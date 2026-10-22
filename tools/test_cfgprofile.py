"""实验 4-10 / 4-11 的配置正确性测试（B1 的判定语就落在这一层）。

B1 要求：对**实验 4-10**（RIP v1/v2 差异正确体现）与**实验 4-11**（OSPF 反掩码、
area 正确）生成的配置**可直接运行**。

「可直接运行」不是靠说的，是靠把生成的配置文本逐条拿出来对：
  ① RIP v1：`version 1` 必须有，`no auto-summary` 必须没有（v1 关不掉汇总）
  ② RIP v2：`version 2` + `no auto-summary` 必须有
  ③ 两者的 network 语句都必须是**有类网络地址**（不带掩码）——IOS 就是这个语法，
     写成 `network 192.168.1.0/26` 是非法命令，PT 会直接 `% Invalid input`
  ④ OSPF：network 用**反掩码**（0.0.0.255 这种），不是子网掩码；area 号跟 DSL 一致
  ⑤ 反掩码要按真实前缀算：/24→0.0.0.255、/26→0.0.0.63、/30→0.0.0.3

不需要 API key，也不联网。
    python tools/test_cfgprofile.py
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import accept, config  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}"
          f"{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


def lines_of(uid_file: str) -> dict[str, list[str]]:
    """按设备切出配置行。用生成器（不走磁盘），保证测的是当前代码。"""
    topo = Topology.load(ROOT / "examples" / uid_file)
    return config.generate_all(topo)


def rip_block(lines: list[str]) -> list[str]:
    """取 router rip 到 `!` 之间的行。"""
    out: list[str] = []
    inside = False
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith("router rip"):
            inside = True
            out.append(s)
            continue
        if inside:
            if s == "!":
                break
            out.append(s)
    return out


def ospf_block(lines: list[str]) -> list[str]:
    out: list[str] = []
    inside = False
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith("router ospf"):
            inside = True
            out.append(s)
            continue
        if inside:
            if s == "!":
                break
            out.append(s)
    return out


def rip_net_ok(line: str) -> bool:
    """RIP 的 network 语句必须是「network + 一个有类网络地址」，两段，不带掩码。

    IOS 里 `network 192.168.1.0/26` 和 `network 192.168.1.0 255.255.255.192`
    都是非法命令，PT 会直接 `% Invalid input detected`，所以这里卡死段数。
    """
    parts = line.split()
    if len(parts) != 2 or parts[0].lower() != "network":
        return False
    addr = parts[1]
    if "/" in addr:
        return False
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", addr))


def expected_rip_nets(topo: Topology) -> set[str]:
    """按接口地址反推每个设备该有的有类 network 语句。"""
    out: set[str] = set()
    for d in topo.devices:
        for itf in d.get("interfaces", []):
            if itf.get("ip") and not itf.get("shutdown"):
                out.add(config.classful(ipaddress.ip_interface(itf["ip"]).ip))
    return out


def main() -> int:
    print("实验 4-10 / 4-11 配置正确性")

    # ------------------------------------------------------------ ① v1
    print("\n[1] RIP v1（uc410_v1 → UC-80）")
    t1 = Topology.load(ROOT / "examples" / "uc410_v1.yaml")
    v1 = lines_of("uc410_v1.yaml")
    v1_rips = {n: rip_block(ls) for n, ls in v1.items() if rip_block(ls)}
    check("三台路由器都出了 router rip", len(v1_rips) == 3, f"实际 {len(v1_rips)} 台")
    for name, blk in v1_rips.items():
        body = " / ".join(blk)
        check(f"{name} 有 version 1", "version 1" in body, body)
        check(f"{name} 没有 no auto-summary", "no auto-summary" not in body, body)
        nets = [x for x in blk if x.startswith("network ")]
        check(f"{name} 有 network 语句", len(nets) >= 2, body)
        bad = [x for x in nets if not rip_net_ok(x)]
        check(f"{name} 的 network 是有类地址（两段、不带掩码）", not bad, f"非法写法 {bad}")

    # ------------------------------------------------------------ ② v2
    print("\n[2] RIP v2（uc410_v2 → UC-81）")
    t2 = Topology.load(ROOT / "examples" / "uc410_v2.yaml")
    v2 = lines_of("uc410_v2.yaml")
    v2_rips = {n: rip_block(ls) for n, ls in v2.items() if rip_block(ls)}
    check("三台路由器都出了 router rip", len(v2_rips) == 3, f"实际 {len(v2_rips)} 台")
    for name, blk in v2_rips.items():
        body = " / ".join(blk)
        check(f"{name} 有 version 2", "version 2" in body, body)
        check(f"{name} 有 no auto-summary", "no auto-summary" in body, body)
        nets = [x for x in blk if x.startswith("network ")]
        bad = [x for x in nets if not rip_net_ok(x)]
        check(f"{name} 的 network 是有类地址（两段、不带掩码）", not bad, f"非法写法 {bad}")

    # ------------------------------------------------------------ ②b 与换算函数对账
    print("\n[2b] network 语句与 classful() 换算对账")
    for tag, topo, cfgs in (("v1", t1, v1), ("v2", t2, v2)):
        want = expected_rip_nets(topo)
        got: set[str] = set()
        for blk in cfgs.values():
            for ln in rip_block(blk):
                if ln.startswith("network "):
                    got.add(ln.split()[1])
        check(f"{tag}：生成的 network 集合 == 接口地址的类网络集合",
              got == want, f"缺 {want - got}，多 {got - want}")
        check(f"{tag}：/25 与 /26 的地址都被汇总成 /24 主网（有类行为）",
              "192.168.16.0" in got and "192.168.16.128" not in got, str(sorted(got)))

    # ------------------------------------------------------------ ③ 对照
    print("\n[3] v1 / v2 是受控对照（拓扑相同，只有路由段不同）")
    d1 = [(d["name"], d.get("role"), d.get("ip")) for d in t1.devices]
    d2 = [(d["name"], d.get("role"), d.get("ip")) for d in t2.devices]
    check("设备清单完全一致", d1 == d2, f"{d1} != {d2}")
    l1 = [(l["from"], l["to"]) for l in t1.links]
    l2 = [(l["from"], l["to"]) for l in t2.links]
    check("链路清单完全一致", l1 == l2, f"{l1} != {l2}")
    r1 = t1.data.get("routing")
    r2 = t2.data.get("routing")
    check("routing 段确实不同", r1 != r2)
    check("v1 段声明 version=1", all(r.get("version") == 1 for r in r1))
    check("v2 段声明 version=2", all(r.get("version") == 2 for r in r2))

    # 唯一差异应该就是 version 与 auto_summary
    diff_keys = set()
    for a, b in zip(r1, r2):
        for k in set(a) | set(b):
            if a.get(k) != b.get(k):
                diff_keys.add(k)
    check("差异只在 version / auto_summary 两个字段",
          diff_keys <= {"version", "auto_summary"},
          f"多出 {diff_keys - {'version', 'auto_summary'}}")

    # ------------------------------------------------------------ ④ OSPF
    print("\n[4] OSPF 反掩码与 area（uc411 → UC-82，手册 4-11）")
    o = lines_of("uc411.yaml")
    o_spfs = {n: ospf_block(ls) for n, ls in o.items() if ospf_block(ls)}
    check("三台路由器都出了 router ospf", len(o_spfs) == 3, f"实际 {len(o_spfs)} 台")
    for name, blk in o_spfs.items():
        check(f"{name} 有 router ospf 100（手册第 24 页指定的进程号）",
              blk and blk[0].split()[-1] == "100", " / ".join(blk))
        nets = [x for x in blk if x.startswith("network ")]
        check(f"{name} 有 network 语句", len(nets) >= 2, " / ".join(blk))
        for ln in nets:
            parts = ln.split()
            # network <net> <wildcard> area <n>
            ok_shape = len(parts) == 5 and parts[3].lower() == "area" and parts[4].isdigit()
            check(f"{name} 「{ln}」是 network/wildcard/area 五段式", ok_shape, ln)
            if ok_shape:
                wc = parts[2]
                check(f"{name} 「{parts[1]}」用的是反掩码（第 3 段不是 255.x 子网掩码）",
                      not wc.startswith("255."), f"反掩码写成了 {wc}")

    all_ospf = " / ".join(x for blk in o_spfs.values() for x in blk)
    # 前缀 → 反掩码的硬性对照：错一位就是整段配置失效
    # 手册 4-11 恰好三种掩码都有：/25 业务网、/26 业务网、/30 互联链路
    for prefix, net, wc, area in (
        (25, "192.168.16.0", "0.0.0.127", "0"),
        (26, "192.168.16.128", "0.0.0.63", "0"),
        (30, "30.0.0.0", "0.0.0.3", "0"),
    ):
        want = f"network {net} {wc} area {area}"
        check(f"/{prefix} → 「{want}」", want in all_ospf, all_ospf)
    check("单区域：全部 area 0（手册 4-11 不是多区域）",
          "area 1" not in all_ospf and "area 100" not in all_ospf, all_ospf)
    # 反向：不能出现子网掩码写法
    check("没有把子网掩码当反掩码写进去",
          "255.255.255.0" not in all_ospf and "255.255.255.192" not in all_ospf
          and "255.255.255.128" not in all_ospf,
          all_ospf)

    # ------------------------------------------------------------ ⑤ 通配
    print("\n[5] 反掩码换算函数（边界）")
    cases = [(0, "255.255.255.255"), (8, "0.255.255.255"), (16, "0.0.255.255"),
             (24, "0.0.0.255"), (25, "0.0.0.127"), (26, "0.0.0.63"),
             (30, "0.0.0.3"), (31, "0.0.0.1"), (32, "0.0.0.0")]
    for p, want in cases:
        got = config.wildcard(p)
        check(f"/{p} → {want}", got == want, f"实际 {got}")

    # ------------------------------------------------------------ ⑥ 有类汇总
    # 这一组验的是实验 4-10 的**核心现象**：同一个主网络的两个子网，
    # RIP v1 会互相丢弃路由（不通），v2 关自动汇总才通。
    # 曾经这里是错的：早期版本用了两个不同主网的 /26，v1 也能通，现象根本没出现。
    print("\n[6] 有类汇总：v1 学不到同主网的另一个子网")

    def rip_routes(yaml_file: str):
        topo = Topology.load(ROOT / "examples" / yaml_file)
        rep = accept.analyze(topo)
        return [a for a in rep.routes if a.code == "R"]

    v1r = rip_routes("uc410_v1.yaml")
    v2r = rip_routes("uc410_v2.yaml")
    # 三台路由器 × 两个主机网段 = 6 条，去掉 R0→/25、R1→/26 这两条直连，
    # **理论上**剩 4 条 RIP 条目：R0→/26、R1→/25、R2→两边的业务网段各一条。
    # 但 v1 汇总后 R2 那两个子网在路由表里是同一行 /24，断言表也随之并成 1 条
    # （accept._merge_summarized），所以 v1 实际是 3 条；v2 不汇总，仍是 4 条。
    check("v1 有 3 条 RIP 路由条目（R2 的两条被汇总合并成一条）",
          len(v1r) == 3, f"实际 {len(v1r)} 条")
    check("v2 有 4 条 RIP 路由条目（不汇总，各子网独立成条）",
          len(v2r) == 4, f"实际 {len(v2r)} 条")

    def at(rows, router):
        return [a for a in rows if a.device == router]

    # R0 学 R1 的业务网段（同主网），R1 学 R0 的 —— 这两条是 v1 学不到的
    check("v1：R0 对 192.168.16.128/26 判为「学不到」",
          [a.ok for a in at(v1r, "R0")] == [False],
          str([(a.prefix, a.ok) for a in at(v1r, "R0")]))
    check("v1：R1 对 192.168.16.0/25 判为「学不到」",
          [a.ok for a in at(v1r, "R1")] == [False],
          str([(a.prefix, a.ok) for a in at(v1r, "R1")]))
    check("v1：R2 能学到且只剩一条（它不在主网 192.168.16.0/24 里，不受汇总影响）",
          len(at(v1r, "R2")) == 1 and all(a.ok for a in at(v1r, "R2")),
          str([(a.prefix, a.ok) for a in at(v1r, "R2")]))
    check("v1：合并后的那条说明里点了两个子网",
          "192.168.16.0/25" in at(v1r, "R2")[0].note
          and "192.168.16.128/26" in at(v1r, "R2")[0].note,
          at(v1r, "R2")[0].note[-60:])
    check("v1：R0/R1 的期望条目是「（无）」",
          all(a.entry == "（无）" for a in at(v1r, "R0") + at(v1r, "R1")),
          str([a.entry for a in at(v1r, "R0") + at(v1r, "R1")]))
    check("v1：R2 学到的是被汇总后的 /24（有类通告）",
          all(a.entry.split()[1] == "192.168.16.0/24"
              for a in at(v1r, "R2")),
          str([a.entry for a in at(v1r, "R2")]))
    check("v1 的说明里点出「主网络」这个原因",
          all("主网络" in a.note for a in at(v1r, "R0")), str(at(v1r, "R0")[0].note[:50]))

    check("v2：全部 4 条都判为成立（关了自动汇总）",
          all(a.ok for a in v2r), str([(a.device, a.prefix, a.ok) for a in v2r]))
    v2_nets = sorted(a.entry.split()[1] for a in v2r)
    check("v2：显示接口真实前缀 /25 与 /26，没有被汇总成 /24",
          v2_nets == ["192.168.16.0/25"] * 2 + ["192.168.16.128/26"] * 2, str(v2_nets))

    # 换算函数本身
    for cidr, want in (("192.168.16.128/26", "192.168.16.0/24"),
                       ("192.168.16.0/25", "192.168.16.0/24"),
                       ("10.0.0.0/30", "10.0.0.0/8"),
                       ("172.16.5.0/26", "172.16.0.0/16"),
                       ("192.168.2.0/24", "192.168.2.0/24")):
        got = accept._classful_cidr(cidr)
        check(f"_classful_cidr({cidr}) → {want}", got == want, f"实际 {got}")

    # ------------------------------------------------------------ ⑦ 三个用例的结论
    print("\n[7] 实验 4-10 / 4-11 的三个结论（手册第 7/9 步）")

    def pairs(yaml_file: str):
        rep = accept.analyze(Topology.load(ROOT / "examples" / yaml_file))
        return rep, {f"{p.src}->{p.dst}": p for p in rep.pairs}

    rv1, pv1 = pairs("uc410_v1.yaml")
    rv2, pv2 = pairs("uc410_v2.yaml")
    ro, po = pairs("uc411.yaml")

    check("① v1：PC0→PC1 **不通**（有类协议不支持 VLSM）",
          pv1["PC0->PC1"].result == "不通", pv1["PC0->PC1"].result)
    check("① v1：反向也不通", pv1["PC1->PC0"].result == "不通", pv1["PC1->PC0"].result)
    check("① v1：不通的原因指向「主网络」而不是「没跑 IGP」",
          "主网络" in pv1["PC0->PC1"].reason, pv1["PC0->PC1"].reason)

    check("② v2：PC0→PC1 **通**", pv2["PC0->PC1"].result == "通", pv2["PC0->PC1"].result)
    check("② v2：反向也通", pv2["PC1->PC0"].result == "通", pv2["PC1->PC0"].result)

    check("③ OSPF：PC0→PC1 **通**", po["PC0->PC1"].result == "通", po["PC0->PC1"].result)

    # 选路差异：RIP 按跳数走串口直连，OSPF 按代价走以太绕行
    t_v2 = {f"{t.src}->{t.dst}": t for t in rv2.traces}
    t_o = {f"{t.src}->{t.dst}": t for t in ro.traces}
    v2_hops = t_v2["PC0->PC1"].hops
    o_hops = t_o["PC0->PC1"].hops
    check("RIP v2 走串口直连（1 跳路由器）",
          v2_hops == ["192.168.16.126", "30.0.0.2", "192.168.16.129"], str(v2_hops))
    check("OSPF 走以太绕行（经 R2，2 跳路由器），因为串口代价 64 > 以太 2",
          o_hops == ["192.168.16.126", "10.0.0.2", "20.0.0.1", "192.168.16.129"], str(o_hops))
    check("两个协议选了**不同**的路径（这正是手册第 9 步要看的）",
          [h for h in v2_hops if "." in h and not h.startswith("192.168.16.1")] !=
          [h for h in o_hops if "." in h and not h.startswith("192.168.16.1")])

    # OSPF 接口代价：与手册「100Mb/s ÷ 带宽，<1 记 1」一致
    check("_iface_cost 串口 = 64（1.544Mb/s）", accept._iface_cost("Serial0/0") == 64,
          str(accept._iface_cost("Serial0/0")))
    check("_iface_cost 以太 = 1（100Mb/s）",
          accept._iface_cost("FastEthernet2/0") == 1,
          str(accept._iface_cost("FastEthernet2/0")))

    # OSPF 代价最短路要真按代价算，不只是跳数
    topo_o = Topology.load(ROOT / "examples" / "uc411.yaml")
    adj_o = accept._router_graph(topo_o)
    check("_path_cost 选中 2 跳绕行而不是 1 跳串口",
          accept._path_cost(adj_o, "R0", "R1") == ["R0", "R2", "R1"],
          str(accept._path_cost(adj_o, "R0", "R1")))
    check("_path（BFS）仍然选 1 跳直连（RIP 用它）",
          accept._path(adj_o, "R0", "R1") == ["R0", "R1"],
          str(accept._path(adj_o, "R0", "R1")))

    # ④ 手册第 10 步「验证 OSPF 等价负载均衡」——之前以为验不了，其实是可算的
    e = {x.device: x for x in ro.ecmps}
    check("④ 只有 R2 出现等价多路径（R0/R1 各有明显更便宜的绕行路）",
          set(e) == {"R2"}, str(sorted(e)))
    if "R2" in e:
        r2 = e["R2"]
        check("④ R2 到 30.0.0.0/30 的两条路径代价都是 65（1+64）",
              r2.prefix == "30.0.0.0/30" and r2.cost == 65,
              f"{r2.prefix} cost={r2.cost}")
        check("④ 两条分支分别是 10.0.0.1（Fa2/0）与 20.0.0.1（Fa3/0）",
              [nh for nh, _ in r2.branches] == ["10.0.0.1", "20.0.0.1"],
              str(r2.branches))

    # ------------------------------------------------------------ ⑧ 端口残留地址
    print("\n[8] pkt 里的端口不能带骨架残留地址（踩过一次真坑）")

    import tempfile

    from pktlab import assemble, pkt as pktmod

    tmp = Path(tempfile.mkdtemp(prefix="cfgprofile_"))
    product = assemble.build(Topology.load(ROOT / "examples" / "uc411.yaml"),
                             tmp / "probe.pkt")
    xml = pktmod.read_pt_file(product.pkt_path)
    xml = xml if isinstance(xml, str) else str(xml)

    stale = []
    for blk in re.findall(r"<DEVICE>.*?</DEVICE>", xml, re.S):
        host = re.search(r"hostname (\w+)</LINE>", blk)
        if not host or not host.group(1).startswith(("R", "SW")):
            continue
        for port in re.findall(r"<PORT>.*?</PORT>", blk, re.S):
            if not re.search(r"<TYPE>e(CopperFastEthernet|Serial)</TYPE>", port):
                continue
            m = re.search(r"<IP>([^<]*)</IP>", port)
            if m and m.group(1).strip():
                stale.append(f"{host.group(1)}={m.group(1)}")
    check("路由器/交换机的端口 IP 全部由 running-config 提供（端口节点留空）",
          not stale, "残留：" + ", ".join(stale))

    conf = "\n".join(
        ln for lines in config.generate_all(Topology.load(ROOT / "examples" / "uc411.yaml")).values()
        for ln in lines
    )
    check("地址确实写进了 running-config（清端口不等于丢配置）",
          "ip address 10.0.0.1 255.255.255.252" in conf and
          "ip address 10.0.0.2 255.255.255.252" in conf, conf[-200:])

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
