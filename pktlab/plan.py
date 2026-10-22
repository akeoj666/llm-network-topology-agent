"""生成 pkt 时配套产出的「IP/VLAN 编址表 + 规划图」。

为什么要有这个：`.pkt` 是给机器用的，点开才知道长什么样；
但**给人看的东西应该是文本**——一张规划图、一张编址表，
交作业、贴实验报告、给同学讲的时候直接能用。

产出四块：
    §1 规划图      按层次画出的 ASCII 拓扑（骨干 → 汇聚 → 接入 → 终端）
    §2 IP 编址表   逐接口：地址、掩码、网段、用途、VLAN
    §3 VLAN 表     VLAN 号、名称、网段、网关(SVI/单臂)、承载设备、端口模式
    §4 设备与策略  每台设备跑什么协议、在哪做 NAT/ACL/默认路由

层次是**从拓扑推出来的**，不是写死的：
    有默认路由的路由器 → 边界/出口层
    其余路由器         → 核心/骨干层
    带 trunk 或 SVI 的交换机 → 汇聚层；其余交换机 → 接入层
    PC / 服务器        → 终端层
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

CABLE_CN = {"serial": "串口线", "cross": "交叉线", "copper": "直通线"}

LAYER_ISP = "ISP/上游"
LAYER_BORDER = "边界/出口"
LAYER_CORE = "核心/骨干"
LAYER_AGG = "汇聚"
LAYER_EDGE = "接入"
LAYER_HOST = "终端"

LAYER_ORDER = [LAYER_ISP, LAYER_BORDER, LAYER_CORE, LAYER_AGG, LAYER_EDGE, LAYER_HOST]


# ---------------------------------------------------------------- 数据
@dataclass
class AddrRow:
    device: str
    port: str
    ip: str = ""
    mask: str = ""
    network: str = ""
    cidr: str = ""
    usage: str = ""
    vlan: str = ""
    note: str = ""


@dataclass
class VlanRow:
    vid: int
    name: str
    cidr: str = ""
    gateway: str = ""
    carrier: str = ""
    ports: str = ""


@dataclass
class DeviceRow:
    name: str
    layer: str
    role: str
    model: str
    protocol: str = ""
    policy: str = ""


@dataclass
class Node:
    dev: str
    parent: str | None = None
    via_port: str = ""     # 父设备侧的端口
    child_port: str = ""   # 本设备侧的端口
    network: str = ""      # 两端之间的网段
    children: list["Node"] = field(default_factory=list)


@dataclass
class Plan:
    uid: str
    title: str
    ascii: str
    addrs: list[AddrRow] = field(default_factory=list)
    vlans: list[VlanRow] = field(default_factory=list)
    devices: list[DeviceRow] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)  # 图上没画出来的冗余链路
    upstream: str = ""


# ---------------------------------------------------------------- 取数
def _ip(cidr: str) -> tuple[str, str, str]:
    """返回 (地址, 掩码, 网段)。没写掩码时掩码留空。"""
    if not cidr:
        return "", "", ""
    if "/" in cidr:
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            return cidr, "", ""
        return str(iface.ip), str(iface.netmask), str(iface.network)
    try:
        ipaddress.ip_address(cidr)
    except ValueError:
        return cidr, "", ""
    return cidr, "", ""


ROLE_CN = {"router": "路由器", "switch": "交换机", "pc": "主机", "server": "服务器"}


def _role(dev: dict) -> str:
    return str(dev.get("role") or "device")


def _role_cn(dev: dict) -> str:
    return ROLE_CN.get(_role(dev), _role(dev))


def _owner_of_ip(topo, ip: str) -> str:
    """这个 IP 属于哪台设备的哪个口（用于找「默认路由指向谁」）。"""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for dev in topo.data.get("devices") or []:
        for itf in dev.get("interfaces") or []:
            cidr = str(itf.get("ip") or "")
            if not cidr or "/" not in cidr:
                continue
            try:
                iface = ipaddress.ip_interface(cidr)
            except ValueError:
                continue
            if iface.ip == addr:
                return str(dev.get("name") or "")
    return ""


def _isp_devices(topo) -> set[str]:
    """默认路由的下一跳所在的那台设备 = 上游（ISP / 互联网侧）。"""
    out: set[str] = set()
    for r in topo.data.get("routing") or []:
        dr = r.get("default_route")
        if not dr:
            continue
        owner = _owner_of_ip(topo, str(dr))
        if owner and owner != r.get("device"):
            out.add(owner)
    return out


def _layer(dev: dict, topo) -> str:
    role = _role(dev)
    if role in ("pc", "server"):
        return LAYER_HOST
    if role == "router":
        if dev.get("name") in _isp_devices(topo):
            return LAYER_ISP
        for r in topo.data.get("routing") or []:
            if r.get("device") == dev["name"] and r.get("default_route"):
                return LAYER_BORDER
        return LAYER_CORE
    # 交换机：带 trunk / 子接口 / SVI 网关的算汇聚
    for itf in dev.get("interfaces") or []:
        if itf.get("mode") == "trunk" or itf.get("dot1q"):
            return LAYER_AGG
    for v in topo.data.get("vlans") or []:
        if v.get("gateway") and _has_vlan_port(dev, v["id"]):
            return LAYER_AGG
    return LAYER_EDGE


def _has_vlan_port(dev: dict, vid: int) -> bool:
    for itf in dev.get("interfaces") or []:
        if itf.get("vlan") == vid or itf.get("dot1q") == vid:
            return True
    return False


def _protocol_of(dev_name: str, topo) -> str:
    for r in topo.data.get("routing") or []:
        if r.get("device") != dev_name:
            continue
        proto = str(r.get("protocol"))
        if proto == "rip":
            v = r.get("version")
            extra = "（关自动汇总）" if r.get("auto_summary") is False else ""
            return f"RIP v{v}{extra}"
        if proto == "ospf":
            return f"OSPF 进程 {r.get('process', 1)}，area {r.get('area', 0)}"
        if proto == "static":
            return "静态路由"
        if proto == "bgp":
            return f"BGP AS {r.get('as')}"
        if proto == "none":
            return "无（三层直连）"
    return ""


def _policy_of(dev_name: str, topo) -> str:
    out: list[str] = []
    for r in topo.data.get("routing") or []:
        if r.get("device") == dev_name and r.get("default_route"):
            out.append(f"默认路由 {r['default_route']}")
    for acl in topo.data.get("acls") or []:
        for ap in acl.get("apply") or []:
            if ap.get("device") == dev_name:
                where = f"{ap.get('interface', '?')} {ap.get('direction', '?')}"
                out.append(f"ACL {acl.get('id')}（{where}）")
    return "；".join(out)


def _port_of(dev_name: str, port: str, topo) -> dict | None:
    for dev in topo.data.get("devices") or []:
        if dev.get("name") != dev_name:
            continue
        for itf in dev.get("interfaces") or []:
            if itf.get("name") == port:
                return itf
    return None


def _iface_of_link(topo, link: dict, side: str) -> tuple[str, str]:
    """返回 (端口名, IP/CIDR)。side 取 'from' 或 'to'。"""
    ref = link.get(side) or ""
    if ":" not in ref:
        return "", ""
    dev, port = ref.split(":", 1)
    itf = _port_of(dev, port, topo)
    cidr = (itf or {}).get("ip") or ""
    return port, cidr


# ---------------------------------------------------------------- 组装
def build(topo) -> Plan:
    devs = topo.data.get("devices") or []
    links = topo.data.get("links") or []
    subnets = {str(s.get("cidr")): s for s in topo.data.get("subnets") or []}

    plan = Plan(uid=topo.meta.get("id", ""), title=topo.meta.get("title", ""), ascii="")

    # ---- 设备表
    for dev in devs:
        plan.devices.append(DeviceRow(
            name=dev.get("name", ""),
            layer=_layer(dev, topo),
            role=_role_cn(dev),
            model=str(dev.get("model") or ""),
            protocol=_protocol_of(dev.get("name", ""), topo),
            policy=_policy_of(dev.get("name", ""), topo),
        ))

    # ---- 邻接表（设备级）
    adj: dict[str, list[tuple[str, dict, str, str]]] = {}
    for ln in links:
        f, t = ln.get("from") or "", ln.get("to") or ""
        if ":" not in f or ":" not in t:
            continue
        fd, fp = f.split(":", 1)
        td, tp = t.split(":", 1)
        net = _link_network(ln, topo)
        adj.setdefault(fd, []).append((td, ln, fp, tp))
        adj.setdefault(td, []).append((fd, ln, tp, fp))

    # ---- 选根：优先有默认路由的（它是出口），否则度数最大的路由器
    isp = next((d.name for d in plan.devices if d.layer == LAYER_ISP), "")
    border = next((d.name for d in plan.devices if d.layer == LAYER_BORDER), "")
    routers = [d.name for d in plan.devices if d.role == "router"]
    root = isp or border or (max(routers, key=lambda n: len(adj.get(n, []))) if routers else "")
    if not root and plan.devices:
        root = plan.devices[0].name

    # ---- 层次顺序（让树从上到下按层走）
    layer_of = {d.name: d.layer for d in plan.devices}

    if isp:
        plan.upstream = isp
    tree = _build_tree(root, adj, layer_of, set(), topo)
    plan.ascii = _render_tree(tree, plan, layer_of, subnets)

    # ---- 没画进树的链路（三角互联的冗余边等）
    drawn = _tree_edges(tree)
    for ln in links:
        f, t = ln.get("from") or "", ln.get("to") or ""
        if ":" not in f or ":" not in t:
            continue
        fd, fp = f.split(":", 1)
        td, tp = t.split(":", 1)
        key = tuple(sorted((fd, td)))
        if key not in drawn:
            cable = CABLE_CN.get(str(ln.get("cable") or ""), "")
            plan.extras.append(
                f"{fd} {fp} ↔ {td} {tp}"
                f"（{_link_network(ln, topo) or '直连'}"
                f"{'，' + cable if cable else ''}）")
    # ---- IP 编址表
    plan.addrs = _addr_rows(topo, subnets)

    # ---- VLAN 表
    plan.vlans = _vlan_rows(topo)

    return plan


def _link_network(ln: dict, topo) -> str:
    """链路的网段：优先用 links[].subnet；没有就从两端的接口地址反推。

    PC—交换机这种二层链路在 DSL 里没有 subnet，但人看图时希望知道
    「这段线落在哪个网段里」，所以从接口地址算出来补上。
    """
    net = str(ln.get("subnet") or "")
    if net:
        return net
    nets = set()
    for side in ("from", "to"):
        ref = ln.get(side) or ""
        if ":" not in ref:
            continue
        dev, port = ref.split(":", 1)
        itf = _port_of(dev, port, topo)
        cidr = str((itf or {}).get("ip") or "")
        if not cidr:
            # PC/服务器把地址写在设备上，不在接口上
            for d in topo.data.get("devices") or []:
                if d.get("name") == dev and d.get("ip"):
                    raw = str(d["ip"])
                    cidr = raw if "/" in raw else f"{raw}/{d.get('mask') or 24}"
                    break
        if "/" in cidr:
            try:
                nets.add(str(ipaddress.ip_interface(cidr).network))
            except ValueError:
                pass
    if len(nets) == 1:
        return next(iter(nets))
    if nets:
        return " / ".join(sorted(nets))
    # 交换机这一侧没有 IP，就看对端主机有没有
    return ""


def _build_tree(root: str, adj, layer_of, seen: set[str], topo) -> Node:
    seen.add(root)
    node = Node(dev=root)
    for child, ln, pport, cport in sorted(adj.get(root, []), key=lambda x: x[0]):
        if child in seen:
            continue
        sub = _build_tree(child, adj, layer_of, seen, topo)
        sub.parent = root
        sub.via_port = pport
        sub.child_port = cport
        sub.network = _link_network(ln, topo)
        node.children.append(sub)
    return node


def _tree_edges(node: Node) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for c in node.children:
        out.add(tuple(sorted((node.dev, c.dev))))
        out |= _tree_edges(c)
    return out


def _render_tree(root: Node, plan: Plan, layer_of, subnets) -> str:
    """把树画成 ASCII。每个设备上方一行写清楚「从哪个口、用哪个网段连过来」。"""
    lines: list[str] = []

    if plan.upstream:
        lines.append("上游网络 / Internet")
        lines.append("│")

    def label_of(dev_name: str) -> str:
        dev = next((d for d in plan.devices if d.name == dev_name), None)
        if dev is None:
            return dev_name
        text = dev.name
        if dev.model:
            text += f"  ({dev.model})"
        tail = [x for x in (dev.layer, dev.protocol, dev.policy) if x]
        if tail:
            text += "   ← " + "；".join(tail)
        return text

    def walk(node: Node, prefix: str, last: bool, is_root: bool) -> None:
        if is_root:
            lines.append(f"{prefix}● {label_of(node.dev)}")
        else:
            seg = f"{node.via_port} ↔ {node.child_port}"
            if node.network:
                seg += f"  {node.network}"
            lines.append(f"{prefix}│ {seg}")
            lines.append(f"{prefix}{'└─' if last else '├─'} {label_of(node.dev)}")
        child_prefix = prefix + ("   " if (last or is_root) else "│  ")
        for i, c in enumerate(node.children):
            walk(c, child_prefix, i == len(node.children) - 1, False)

    walk(root, "", True, True)
    return "\n".join(lines)


def _addr_rows(topo, subnets) -> list[AddrRow]:
    rows: list[AddrRow] = []
    link_by_pair = {}
    for ln in topo.data.get("links") or []:
        f, t = ln.get("from") or "", ln.get("to") or ""
        if ":" in f and ":" in t:
            link_by_pair[(f, t)] = ln

    for dev in topo.data.get("devices") or []:
        name = dev.get("name", "")
        role = _role(dev)
        if role in ("pc", "server"):
            ip, mask, _net = _ip(str(dev.get("ip") or ""))
            rows.append(AddrRow(
                device=name, port="(网卡)", ip=ip, mask=mask,
                network=str(ipaddress.ip_interface(f"{ip}/{mask}").network) if ip and mask else "",
                cidr=f"{ip}/{mask}" if ip else "",
                usage="终端", note=f"默认网关 {dev.get('gateway')}" if dev.get("gateway") else "",
            ))
            continue

        for itf in dev.get("interfaces") or []:
            port = itf.get("name", "")
            cidr = str(itf.get("ip") or "")
            ip, mask, _n = _ip(cidr)
            net = ""
            if ip and mask:
                net = str(ipaddress.ip_interface(f"{ip}/{mask}").network)
            vlan = ""
            if itf.get("dot1q"):
                vlan = f"{itf['dot1q']}（单臂 dot1Q）"
            elif itf.get("vlan"):
                vlan = f"{itf['vlan']}"
            usage = ""
            note = ""
            if itf.get("shutdown"):
                usage, note = "备用", "已 shutdown"
            elif ip:
                # 是不是业务网段（带网关）还是点到点互联
                sub = subnets.get(net)
                if sub and sub.get("gateway") == ip:
                    usage = "业务网关"
                elif mask in ("255.255.255.252", "255.255.255.254"):
                    usage = "互联"
                elif sub:
                    usage = "业务"
                else:
                    usage = "互联"
            else:
                usage = "未配地址"
            rows.append(AddrRow(
                device=name, port=port, ip=ip, mask=mask, network=net,
                cidr=cidr, usage=usage, vlan=vlan, note=note,
            ))
    return rows


def _vlan_rows(topo) -> list[VlanRow]:
    rows: list[VlanRow] = []
    declared = {int(v["id"]): v for v in topo.data.get("vlans") or []}
    used: dict[int, list[str]] = {}
    for dev in topo.data.get("devices") or []:
        for itf in dev.get("interfaces") or []:
            for vid in (itf.get("vlan"), itf.get("dot1q")):
                if vid:
                    used.setdefault(int(vid), []).append(
                        f"{dev.get('name')} {itf.get('name')}")

    # 子网里声明的 vlan
    for s in topo.data.get("subnets") or []:
        if s.get("vlan"):
            used.setdefault(int(s["vlan"]), [])

    for vid in sorted(set(list(declared) + list(used))):
        d = declared.get(vid, {})
        cidr = str(d.get("cidr") or "")
        gw = str(d.get("gateway") or "")
        # 子网里也声明了 VLAN 的话，网段与网关以子网为准（那里才是真实网关地址）
        for s in topo.data.get("subnets") or []:
            if int(s.get("vlan") or 0) == vid:
                cidr = cidr or str(s.get("cidr") or "")
                gw = gw or str(s.get("gateway") or "")
        rows.append(VlanRow(
            vid=vid,
            name=str(d.get("name") or f"VLAN{vid}"),
            cidr=cidr, gateway=gw,
            carrier="、".join(sorted(set(used.get(vid, [])))) or "—",
        ))
    return rows


# ---------------------------------------------------------------- 渲染
def render_markdown(plan: Plan) -> str:
    L: list[str] = [f"# {plan.uid} {plan.title} — IP/VLAN 编址规划", ""]

    L += ["## 1 规划图", "", "```text", plan.ascii, "```", ""]
    if plan.extras:
        L += ["图上没画出来、但拓扑里存在的链路（多为三角互联的冗余边）：", ""]
        L += [f"- {e}" for e in plan.extras]
        L += [""]

    L += ["## 2 IP 编址表", "",
          "| 设备 | 端口 | IP 地址 | 掩码 | 所属网段 | CIDR | 用途 | VLAN | 说明 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in plan.addrs:
        L.append(f"| {r.device} | {r.port} | {r.ip or '—'} | {r.mask or '—'} | "
                 f"{r.network or '—'} | {r.cidr or '—'} | {r.usage} | {r.vlan or '—'} | {r.note or ''} |")
    L += [""]

    L += ["## 3 VLAN 与网关表", "",
          "| VLAN | 名称 | 网段 | 网关(SVI/单臂) | 承载接口 |", "|---|---|---|---|---|"]
    if plan.vlans:
        for v in plan.vlans:
            L.append(f"| {v.vid} | {v.name} | {v.cidr or '—'} | {v.gateway or '—'} | {v.carrier} |")
    else:
        L.append("| — | 本拓扑未划分 VLAN | — | — | — |")
    L += [""]

    L += ["## 4 设备与策略", "",
          "| 设备 | 层次 | 角色 | 型号 | 路由协议 | 关键策略 |", "|---|---|---|---|---|---|"]
    for d in plan.devices:
        L.append(f"| {d.name} | {d.layer} | {d.role} | {d.model} | "
                 f"{d.protocol or '—'} | {d.policy or '—'} |")
    L += [""]

    L += ["## 5 这份规划怎么读", "",
          "- **层次**是从拓扑关系推出来的，不是手工指定的：有默认路由的路由器算出口层，",
          "  其余路由器算核心层，带 trunk 或单臂的交换机算汇聚层，纯 access 口交换机算接入层。",
          "- **用途**列区分「业务网关 / 业务 / 互联 / 备用」：",
          "  掩码是 /30 或 /31 的按点到点互联算，其余按业务网段算。",
          "- **VLAN** 列带「（单臂 dot1Q）」的表示路由器子接口封装，"
          "交换机侧同一个 VLAN 号是 access 口。",
          "- 表里没有出现的接口，是没有配地址的空口（`shutdown` 的会标成「备用」）。",
          ""]
    return "\n".join(L)
