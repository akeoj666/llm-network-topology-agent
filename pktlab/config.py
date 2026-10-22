"""DSL → Cisco IOS 配置。

实测得到的硬约束（见 docs 与 memory）：
  1. Packet Tracer 回放配置时，**遇到第一行非法命令就会中止后续全部行**，
     所以这里生成的每一行都必须是该设备真实支持的。
  2. 每个接口必须显式 `no shutdown`，否则接口停在 administratively down。
  3. 接口名由硬件决定：Router-PT-Empty 插串口模块后是 Serial0/0、Serial1/0、
     FastEthernet2/0、FastEthernet3/0（没有 Fa0/0）。
"""

from __future__ import annotations

import ipaddress

from .dsl import Topology

# 硬件端口目录
CATALOG: dict[str, dict[str, list[str]]] = {
    "Router-PT-Empty": {
        "serial": ["Serial0/0", "Serial1/0"],
        "lan": ["FastEthernet2/0", "FastEthernet3/0"],
    },
    "2911": {
        "serial": [],
        "lan": ["GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2"],
    },
    "2960-24TT": {
        "lan": [f"FastEthernet0/{i}" for i in range(1, 25)] + ["GigabitEthernet0/1", "GigabitEthernet0/2"],
    },
    "PC-PT": {"lan": ["FastEthernet0"]},
}

ROLE_MODEL = {
    "router": "Router-PT-Empty",
    "switch": "2960-24TT",
    "pc": "PC-PT",
    "server": "PC-PT",
}


def netmask(prefix: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)


def wildcard(prefix: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").hostmask)


def classful(ip: ipaddress.IPv4Address) -> str:
    first = int(str(ip).split(".")[0])
    if first < 128:
        return f"{first}.0.0.0"
    if first < 192:
        return ".".join(str(ip).split(".")[:2]) + ".0.0"
    return ".".join(str(ip).split(".")[:3]) + ".0"


def dce_ports(topo: Topology) -> dict[str, set[str]]:
    """串行链路的 DCE 端需要 clock rate。DSL 里 dce: from|to，默认 to。"""
    out: dict[str, set[str]] = {}
    for ln in topo.links:
        if ln.get("cable") != "serial":
            continue
        side = ln.get("dce", "to")
        ref = ln["from"] if side == "from" else ln["to"]
        dev, _, port = ref.partition(":")
        out.setdefault(dev, set()).add(port)
    return out


def _itf_blocks(topo: Topology, dev: dict, dce: dict[str, set[str]]) -> list[str]:
    groups: dict[str, list[tuple[int, str]]] = {}
    for acl in topo.data.get("acls", []):
        for ap in acl.get("apply", []):
            if ap.get("device") != dev["name"]:
                continue
            groups.setdefault(ap["interface"], []).append((acl["id"], ap["direction"]))

    lines: list[str] = []
    for itf in dev.get("interfaces", []):
        name = itf["name"]
        lines.append(f"interface {name}")
        # 单臂路由子接口：encapsulation 必须在 ip address 之前
        if itf.get("dot1q"):
            lines.append(f" encapsulation dot1Q {itf['dot1q']}")
        if itf.get("ip"):
            ifc = ipaddress.ip_interface(itf["ip"])
            lines.append(f" ip address {ifc.ip} {netmask(ifc.network.prefixlen)}")
        elif not itf.get("dot1q"):
            # 子接口继承了父接口的二层封装，不写 no ip address（PT 回放会报错）
            lines.append(" no ip address")
        if name in dce.get(dev["name"], set()):
            lines.append(" clock rate 2000000")
        # ACL 必须挂在 ip address 之后
        for aid, direction in groups.get(name, []):
            lines.append(f" ip access-group {aid} {direction}")
        if not itf.get("shutdown", False):
            lines.append(" no shutdown")
        else:
            lines.append(" shutdown")
        lines.append("!")
    return lines


def _addr(text: str | None) -> str:
    """DSL 地址 → IOS ACL 写法。any / host x.x.x.x / 网络号 + 反掩码。"""
    if not text or text == "any":
        return "any"
    net = ipaddress.ip_network(text, strict=False)
    if net.prefixlen == 32:
        return f"host {net.network_address}"
    return f"{net.network_address} {wildcard(net.prefixlen)}"


def _acl_lines(topo: Topology, dev: dict) -> list[str]:
    """在「应用了这条 ACL 的设备」上生成 access-list 定义。

    IOS 里 access-list 是本地概念：哪台设备上挂了 ip access-group，
    哪台设备就必须自己有一份定义。定义要排在 interface 之前，
    否则回放到 `ip access-group` 那一行会找不到 ACL。
    """
    lines: list[str] = []
    for acl in topo.data.get("acls", []):
        if not any(ap.get("device") == dev["name"] for ap in acl.get("apply", [])):
            continue
        aid, kind = acl["id"], acl["type"]
        for rule in acl.get("rules", []):
            action = rule["action"]
            if kind == "standard":
                lines.append(f"access-list {aid} {action} {_addr(rule.get('source'))}")
                continue
            proto = rule.get("protocol", "ip")
            line = f"access-list {aid} {action} {proto} {_addr(rule.get('source'))} {_addr(rule.get('destination'))}"
            if rule.get("port") is not None:
                line += f" eq {rule['port']}"
            lines.append(line)
        lines.append("!")
    return lines


def _routing_block(topo: Topology, dev: dict) -> list[str]:
    entries = [r for r in topo.data.get("routing", []) if r.get("device") == dev["name"]]
    if not entries:
        return []
    r = entries[0]
    proto = r["protocol"]
    if proto == "none":
        return []
    if proto == "rip":
        ver = r.get("version", 2)
        nets = r.get("networks") or _derive_rip_networks(dev)
        out = ["router rip", f" version {ver}"]
        if ver == 2 and r.get("auto_summary") is False:
            out.append(" no auto-summary")
        out += [f" network {n}" for n in nets]
        return out + ["!"]
    if proto == "ospf":
        proc = r.get("process", 1)
        area = r.get("area", 0)
        pairs = _derive_ospf_networks(dev, area) if not r.get("networks") else [
            (n, wildcard(int(n.split("/")[1])) if "/" in n else "0.0.0.255", area) for n in r["networks"]
        ]
        out = [f"router ospf {proc}"]
        for net, wc, ar in pairs:
            out.append(f" network {net} {wc} area {ar}")
        for ar in r.get("stub_areas", []):
            out.append(f" area {ar} stub")
        return out + ["!"]
    if proto == "static":
        out = []
        for route in r.get("routes", []):
            # 浮动静态：AD 写在第 4 段，不写就是默认 1
            ad = route.get("admin_distance")
            line = f"ip route {route['network']} {route['mask']} {route['next_hop']}"
            out.append(f"{line} {ad}" if ad else line)
        if r.get("default_route"):
            ad = r.get("admin_distance")
            line = f"ip route 0.0.0.0 0.0.0.0 {r['default_route']}"
            out.append(f"{line} {ad}" if ad else line)
        return out + ["!"] if out else []
    if proto == "bgp":
        asn = r.get("as", 100)
        out = [f"router bgp {asn}"]
        for nb in r.get("neighbors", []):
            out.append(f" neighbor {nb['ip']} remote-as {nb['remote_as']}")
        for n in r.get("networks", []):
            net = ipaddress.ip_network(n, strict=False)
            out.append(f" network {net.network_address} mask {net.netmask}")
        return out + ["!"]
    return []


def _derive_rip_networks(dev: dict) -> list[str]:
    nets: list[str] = []
    for itf in dev.get("interfaces", []):
        if not itf.get("ip"):
            continue
        ifc = ipaddress.ip_interface(itf["ip"])
        n = classful(ifc.ip)
        if n not in nets:
            nets.append(n)
    return nets


def _derive_ospf_networks(dev: dict, default_area: int = 0) -> list[tuple[str, str, int]]:
    """按接口反推 network 语句。接口可以单独指定 area（多区域 OSPF 的 ABR 需要）。"""
    out: list[tuple[str, str, int]] = []
    for itf in dev.get("interfaces", []):
        if not itf.get("ip"):
            continue
        net = ipaddress.ip_interface(itf["ip"]).network
        out.append((str(net.network_address), wildcard(net.prefixlen), itf.get("area", default_area)))
    return out


def _switch_config(topo: Topology, dev: dict) -> list[str]:
    """交换机配置。注意 hostname 已由 generate() 的 head 写过，这里不要再写一遍。"""
    lines: list[str] = []
    vlans = {v["id"]: v for v in topo.data.get("vlans", [])}
    for vid, v in sorted(vlans.items()):
        lines += [f"vlan {vid}", f" name {v['name']}", "!"]
    for itf in dev.get("interfaces", []):
        lines.append(f"interface {itf['name']}")
        if itf.get("mode") == "trunk":
            lines += [" switchport mode trunk"]
            allowed = itf.get("trunk_vlans")
            if allowed:
                lines.append(" switchport trunk allowed vlan "
                             + ",".join(str(v) for v in allowed))
        elif itf.get("vlan"):
            lines += [" switchport mode access", f" switchport access vlan {itf['vlan']}"]
        lines += [" no shutdown", "!"]
    # SVI 只给**真正做这个 VLAN 网关的那台交换机**开。
    # 判据：网关地址就是本设备某个接口的地址。之前只判「这个 VLAN 有没有网关」，
    # 结果所有交换机都开一遍 SVI——园区网里 5 台交换机各开 5 个 SVI，明显不对。
    my_ips = {str(i.get("ip") or "").split("/")[0]
              for i in dev.get("interfaces", []) if i.get("ip")}
    for vid, v in sorted(vlans.items()):
        if not (v.get("cidr") and v.get("gateway")):
            continue
        gw = str(v["gateway"])
        if gw not in my_ips:
            continue
        ifc = ipaddress.ip_interface(f"{gw}/{v['cidr'].split('/')[1]}")
        lines += [
            f"interface vlan {vid}",
            f" ip address {ifc.ip} {netmask(ifc.network.prefixlen)}",
            " no shutdown",
            "!",
        ]
    return lines


def generate(topo: Topology, name: str) -> list[str]:
    """生成单台设备的完整配置行。"""
    dev = topo.device(name)
    if dev is None:
        raise KeyError(name)
    if dev["role"] in ("pc", "server"):
        return []  # 主机没有 CLI 配置，网卡参数另外写进 pkt

    dce = dce_ports(topo)
    head = [
        "version 12.2",
        "no service timestamps log datetime msec",
        "no service timestamps debug datetime msec",
        "no service password-encryption",
        "!",
        f"hostname {name}",
        "!",
        "no ip cef",
        "no ipv6 cef",
        "!",
    ]
    if dev["role"] == "switch":
        return head + _switch_config(topo, dev) + ["end"]

    body = _itf_blocks(topo, dev, dce)
    acl = _acl_lines(topo, dev)
    routing = _routing_block(topo, dev)
    tail = [
        "ip classless",
        "!",
        "line con 0",
        "!",
        "line vty 0 4",
        " login",
        "!",
        "end",
    ]
    return head + acl + body + routing + tail


def generate_all(topo: Topology) -> dict[str, list[str]]:
    return {d["name"]: generate(topo, d["name"]) for d in topo.devices}
