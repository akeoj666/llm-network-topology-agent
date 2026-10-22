"""静态验收：不打开 Packet Tracer，就从拓扑模型推算「谁应该能通、谁应该不通」。

为什么不是真跑一遍：
  Packet Tracer 没有可脚本化的仿真接口（没有 CLI、没有 Python API 可以触发 ping），
  所以「自动验收」只能是**建模推算 + 配置一致性检查**，产出预期矩阵，
  再到 PT 里抽样核对。这一点在报告里必须写清楚，不能包装成「自动跑通」。

推算依据（全部来自已生成的配置，不是拍脑袋）：
  1. 同网段 → 二层直通（VLAN 场景下要求同 VLAN）
  2. 跨网段 → 找路由器路径；路径上每台路由器都要能把包转发出去
     - 全网跑同一种 IGP（rip/ospf/bgp）且路由器图连通 → 认为已收敛，全互通
     - 否则只看直连网段 + 静态路由
  3. 路径上任何一台路由器挂的 ACL 拒绝 → 判「被 ACL 拒绝」
     ACL 按顺序匹配，第一条命中即生效，末尾隐含 deny any

除了推算，还查配置自洽性（`_static_checks`）：
  网关有效性 / 点对点两端同网段 / 业务网段有三层接口 / ACL 有 permit 兜底 /
  IGP 覆盖连通图 / 二层配套（access 口 VLAN、trunk 上联、dot1Q 子接口三者对上）。

用例可以用 `expect.reach` / `expect.block` 声明预期，据此判定 PASS/FAIL。
反向测试见 tools/test_accept.py，确认这些检查不是永远通过的摆设。
"""

from __future__ import annotations

import heapq
import ipaddress
import re
from collections import deque
from dataclasses import dataclass, field

from .dsl import Topology

IGP = {"rip", "ospf", "bgp"}


# ---------------------------------------------------------------- 基础索引
def _is_router(topo: Topology, name: str) -> bool:
    d = topo.device(name)
    return bool(d and d["role"] == "router")


def _routers(topo: Topology) -> list[str]:
    return [d["name"] for d in topo.devices if d["role"] == "router"]


def _hosts(topo: Topology) -> list[dict]:
    return [d for d in topo.devices if d["role"] in ("pc", "server") and d.get("ip")]


def _iface_net(topo: Topology) -> dict[str, list[tuple[str, str]]]:
    """网段 → [(设备, 接口)]，只算三层接口。"""
    nets: dict[str, list[tuple[str, str]]] = {}
    for d in topo.devices:
        for itf in d.get("interfaces", []):
            if not itf.get("ip"):
                continue
            net = str(ipaddress.ip_interface(itf["ip"]).network)
            nets.setdefault(net, []).append((d["name"], itf["name"]))
    return nets


def _router_graph(topo: Topology) -> dict[str, dict[str, tuple[str, str]]]:
    """路由器互联图：a → {b: (a 的接口, b 的接口)}。"""
    nets = _iface_net(topo)
    adj: dict[str, dict[str, tuple[str, str]]] = {r: {} for r in _routers(topo)}
    for _, members in nets.items():
        rs = [(n, i) for n, i in members if n in adj]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, ai = rs[i]
                b, bi = rs[j]
                adj[a][b] = (ai, bi)
                adj[b][a] = (bi, ai)
    return adj


def _routing(topo: Topology, name: str) -> dict:
    for r in topo.data.get("routing", []):
        if r.get("device") == name:
            return r
    return {}


def _gateway_router(topo: Topology, gw: str) -> tuple[str, str] | None:
    """默认网关对应哪台路由器的哪个接口。"""
    try:
        target = ipaddress.ip_address(gw)
    except ValueError:
        return None
    for d in topo.devices:
        if d["role"] != "router":
            continue
        for itf in d.get("interfaces", []):
            if not itf.get("ip"):
                continue
            if ipaddress.ip_interface(itf["ip"]).ip == target:
                return d["name"], itf["name"]
    return None


def _iface_facing(topo: Topology, router: str, net: str) -> str | None:
    d = topo.device(router)
    for itf in d.get("interfaces", []) if d else []:
        if itf.get("ip") and str(ipaddress.ip_interface(itf["ip"]).network) == net:
            return itf["name"]
    return None


def _classful_cidr(cidr: str) -> str:
    """把任意前缀折成它的主网络（有类）写法。

    192.168.2.0/26 → 192.168.2.0/24；10.0.0.0/30 → 10.0.0.0/8。

    为什么需要这个：RIP v1 的通告里不带掩码，收方只能按主网络归类；
    RIP v2 开了自动汇总（默认）也一样。所以路由表里显示的前缀是**主网络**，
    不是接口上那台 /26 —— 这正是实验 4-10 要看的差异。
    """
    net = ipaddress.ip_network(cidr, strict=False)
    first = int(str(net.network_address).split(".")[0])
    pfx = 8 if first < 128 else (16 if first < 192 else 24)
    if pfx == net.prefixlen:
        return str(net)
    return str(ipaddress.ip_network(
        f"{net.network_address}/{pfx}", strict=False
    ))


def _rip_summarizes(rt: dict) -> bool:
    """这台路由器的 RIP 会不会按主网络汇总。

    v1 只能汇总；v2 只有显式 `no auto-summary` 才不汇总。
    """
    if rt.get("protocol") != "rip":
        return False
    ver = rt.get("version", 2)
    return not (ver == 2 and rt.get("auto_summary") is False)


def _rip_summary_blocks(topo: Topology, router: str, dst_net: str) -> bool:
    """RIP 有类汇总造成的「明明有路却学不到」——实验 4-10 的核心现象。

    规则（IOS 的实际行为）：路由器如果**直连**主网络 M，那么它只从
    「也在 M 里」的接口学习 M 的子网路由；从别的接口发来的、关于 M 子网的
    更新一律丢弃（因为它是按主网络通告的，跟本机直连的 M 撞车）。

    典型后果：R0 直连 192.168.16.0/25，R1 直连 192.168.16.128/26，
    两者同属主网 192.168.16.0/24。RIP v1 下 R0 认为整个 /24 都在自己家门口，
    于是拒绝 R1 通告的 192.168.16.0/24 → PC0 ping 不通 PC1。
    v2 关掉自动汇总后按真实前缀通告，这条路才通。

    只影响「从别处学来的同主网子网」，直连的照旧（`_iface_facing` 已先判过）。
    """
    if not _rip_summarizes(_routing(topo, router)):
        return False
    dst_major = _classful_cidr(dst_net)
    d = topo.device(router)
    for itf in (d or {}).get("interfaces", []):
        if not itf.get("ip") or itf.get("shutdown"):
            continue
        here = str(ipaddress.ip_interface(itf["ip"]).network)
        if _classful_cidr(here) == dst_major:
            return True
    return False


# ---------------------------------------------------------------- ACL
def _acl_rules(topo: Topology, dev: str, ifname: str, direction: str) -> list[dict]:
    """取出挂在(dev, ifname, direction)上的规则，按定义顺序展开。"""
    rules: list[dict] = []
    for acl in topo.data.get("acls", []):
        hit = any(
            ap.get("device") == dev
            and ap.get("interface") == ifname
            and ap.get("direction") == direction
            for ap in acl.get("apply", [])
        )
        if not hit:
            continue
        for rule in acl.get("rules", []):
            rules.append({**rule, "_acl": acl["id"]})
    return rules


def _match_net(expr: str | None, ip: ipaddress.IPv4Address) -> bool:
    if not expr or expr == "any":
        return True
    return ip in ipaddress.ip_network(expr, strict=False)


def _eval_acl(rules: list[dict], src: ipaddress.IPv4Address, dst: ipaddress.IPv4Address) -> tuple[str, dict | None]:
    """返回 (permit|deny, 命中的规则)。ICMP 不会被带 port 的规则命中。"""
    for rule in rules:
        proto = rule.get("protocol", "ip")
        if proto not in ("ip", "icmp"):
            continue
        if rule.get("port") is not None:
            continue
        if not _match_net(rule.get("source"), src):
            continue
        if not _match_net(rule.get("destination"), dst):
            continue
        return rule["action"], rule
    return "deny", None  # 末尾隐含 deny


# ---------------------------------------------------------------- 结果
@dataclass
class Pair:
    src: str
    dst: str
    src_ip: str
    dst_ip: str
    result: str  # 通 / 不通
    reason: str
    path: list[str] = field(default_factory=list)
    # 包在哪台路由器上被终止（无路由回 unreachable / 被 ACL 丢弃）。
    # traceroute 的跳序要截到这里，不能把后面根本没到达的路由器也列进去。
    stop_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == "通"


@dataclass
class Assertion:
    """DSL 里声明的预期 vs 验收器推算出来的结果。"""

    src: str
    dst: str
    want: str  # 通 / 不通
    got: str  # 通 / 不通 / 未推算
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.want == self.got


@dataclass
class Trace:
    """一条 traceroute 路径断言。

    Packet Tracer 里 tracert 打印的是「每台路由器**接收**该包的接口地址」，
    最后一行是目的主机地址。所以跳序列 = [源主机网关] + [路径上第 2..n 台
    路由器的入口地址] + [目的 IP]，其中第一跳就是网关那台路由器的入口。
    """

    src: str
    dst: str
    dst_ip: str
    hops: list[str] = field(default_factory=list)
    routers: list[str] = field(default_factory=list)
    result: str = "通"  # 通 / 断
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.result == "通"

    def text(self) -> str:
        return " → ".join(self.hops) if self.hops else "（无跳）"


@dataclass
class RouteAssert:
    """一条 `show ip route` 关键条目断言。"""

    device: str
    prefix: str
    code: str  # C / R / O / B / S
    entry: str  # 预期在 PT 里看到的那一行
    ok: bool = True
    note: str = ""


@dataclass
class Ecmp:
    """一条「等价多路径」断言（OSPF 的 ECMP，实验 4-11 第 10 步的验证点）。

    判定语是「两条路由代价相同，流量会交替走」。代价相同这件事是能算出来的，
    「会交替」是数据面的行为——PT 里用 `ping` 多打几次看事件列表就能看到。
    """

    device: str
    prefix: str  # 目的网段（通常是路由器之间的传输网段）
    cost: int  # 每条路径的共同代价
    branches: list[tuple[str, str]]  # [(下一跳地址, 出口接口), ...]
    note: str = ""


@dataclass
class Report:
    uid: str
    title: str
    pairs: list[Pair] = field(default_factory=list)
    checks: list[tuple[bool, str]] = field(default_factory=list)
    asserts: list[Assertion] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)
    routes: list[RouteAssert] = field(default_factory=list)
    ecmps: list[Ecmp] = field(default_factory=list)

    @property
    def failed_checks(self) -> list[str]:
        return [m for ok, m in self.checks if not ok]

    @property
    def failed_asserts(self) -> list[Assertion]:
        return [a for a in self.asserts if not a.ok]

    @property
    def n_reach(self) -> int:
        return sum(1 for p in self.pairs if p.ok)

    @property
    def n_block(self) -> int:
        return len(self.pairs) - self.n_reach

    @property
    def passed(self) -> bool:
        """有断言就以断言为准；没断言就只能看静态检查。"""
        if self.asserts:
            return not self.failed_asserts
        return not self.failed_checks


# ---------------------------------------------------------------- 主流程
def analyze(topo: Topology) -> Report:
    rep = Report(topo.meta.get("id", ""), topo.meta.get("title", ""))
    routers = _routers(topo)
    adj = _router_graph(topo)
    nets = _iface_net(topo)
    hosts = _hosts(topo)

    # 全网是不是都在跑 IGP
    igp_all = bool(routers) and all(_routing(topo, r).get("protocol") in IGP for r in routers)
    graph_connected = _connected(adj) if routers else False

    def can_reach_net(router: str, dst_net: str, depth: int = 0) -> bool:
        if _iface_facing(topo, router, dst_net):
            return True
        # 有类汇总导致的「学不到」：必须在 IGP 那条捷径之前判，
        # 否则 igp_all 会直接返回 True，实验 4-10 的现象就永远出不来。
        if _rip_summary_blocks(topo, router, dst_net):
            return False
        if igp_all and graph_connected:
            return True
        rt = _routing(topo, router)
        if rt.get("protocol") == "static":
            if rt.get("default_route"):
                return True
            for route in rt.get("routes", []):
                try:
                    if ipaddress.ip_network(dst_net).subnet_of(
                        ipaddress.ip_network(f"{route['network']}/{route['mask']}", strict=False)
                    ):
                        return True
                except (ValueError, KeyError):
                    continue
        return False

    for src in hosts:
        for dst in hosts:
            if src["name"] == dst["name"]:
                continue
            sip = ipaddress.ip_interface(src["ip"]).ip
            dip = ipaddress.ip_interface(dst["ip"]).ip
            snet = str(ipaddress.ip_interface(src["ip"]).network)
            dnet = str(ipaddress.ip_interface(dst["ip"]).network)

            # ① 同网段：二层直通
            if snet == dnet:
                rep.pairs.append(Pair(src["name"], dst["name"], str(sip), str(dip), "通", "同网段，二层直通"))
                continue

            # ② 找源侧路由器
            gw = src.get("gateway")
            if not gw:
                rep.pairs.append(Pair(src["name"], dst["name"], str(sip), str(dip), "不通", "源主机没配网关"))
                continue
            gr = _gateway_router(topo, gw)
            if gr is None:
                rep.pairs.append(
                    Pair(src["name"], dst["name"], str(sip), str(dip), "不通", f"网关 {gw} 不对应任何路由器接口")
                )
                continue
            r_src, if_src = gr

            # ③ 找目的侧路由器
            dr = _gateway_router(topo, dst.get("gateway", "")) if dst.get("gateway") else None
            if dr is None:
                # 目的网段可能直接挂在某台路由器上
                owner = [n for n, _ in nets.get(dnet, []) if n in adj]
                if not owner:
                    rep.pairs.append(
                        Pair(src["name"], dst["name"], str(sip), str(dip), "不通", f"网段 {dnet} 没有三层接口")
                    )
                    continue
                r_dst = owner[0]
            else:
                r_dst = dr[0]

            path = _path_for(topo, adj, r_src, r_dst)
            if path is None:
                rep.pairs.append(
                    Pair(src["name"], dst["name"], str(sip), str(dip), "不通", f"{r_src} 到 {r_dst} 没有路由通路")
                )
                continue

            # ④ 路径上每台路由器都要能转发
            blocked = next((r for r in path if not can_reach_net(r, dnet)), None)
            if blocked:
                if _rip_summary_blocks(topo, blocked, dnet):
                    why = (
                        f"{blocked} 直连主网络 {_classful_cidr(dnet)}，"
                        f"RIP v{_routing(topo, blocked).get('version', 2)} 按主网络通告不带掩码，"
                        f"于是丢弃了 {dnet} 的路由（有类协议的固有行为，不是配置写错）"
                    )
                else:
                    why = f"{blocked} 上没有 {dnet} 的路由（既没跑 IGP 也没有匹配的静态路由）"
                rep.pairs.append(
                    Pair(src["name"], dst["name"], str(sip), str(dip), "不通",
                         why, path, blocked)
                )
                continue

            # ⑤ ACL
            verdict = None
            for idx, r in enumerate(path):
                if idx == 0:
                    in_if = if_src
                else:
                    in_if = adj[path[idx - 1]][r][1]
                if idx == len(path) - 1:
                    out_if = _iface_facing(topo, r, dnet)
                else:
                    out_if = adj[r][path[idx + 1]][0]

                for ifname, direction in ((in_if, "in"), (out_if, "out")):
                    if not ifname:
                        continue
                    rules = _acl_rules(topo, r, ifname, direction)
                    if not rules:
                        continue
                    action, hit = _eval_acl(rules, sip, dip)
                    if action == "deny":
                        desc = (
                            f"ACL {hit['_acl']} 规则「{hit['action']}」命中"
                            if hit
                            else f"ACL {rules[0]['_acl']} 未命中任何规则，末尾隐含 deny"
                        )
                        verdict = (f"{r} {ifname} {direction} 方向 {desc}", path, r)
                        break
                if verdict:
                    break

            if verdict:
                rep.pairs.append(
                    Pair(src["name"], dst["name"], str(sip), str(dip), "不通",
                         verdict[0], verdict[1], verdict[2])
                )
            else:
                rep.pairs.append(
                    Pair(src["name"], dst["name"], str(sip), str(dip), "通", "路由可达，无 ACL 拦截", path)
                )

    rep.checks = _static_checks(topo, nets, adj, igp_all)
    rep.asserts = _eval_expect(topo, rep)
    # C2：路径断言 + 路由表断言（数据本来就有，这里显式落成可对照的两张表）
    rep.traces = _traces(topo, rep, adj)
    rep.routes = _route_asserts(topo, adj, nets, igp_all, graph_connected)
    # 4-11 第 10 步：等价多路径（代价相同、下一跳不同）
    rep.ecmps = _ecmp_asserts(topo, adj, nets)
    return rep


def _eval_expect(topo: Topology, rep: Report) -> list[Assertion]:
    """把 DSL 里声明的预期，跟推算结果逐条比对。"""
    spec = topo.data.get("expect") or {}
    index = {(p.src, p.dst): p for p in rep.pairs}
    out: list[Assertion] = []
    for key, want in (("reach", "通"), ("block", "不通")):
        for item in spec.get(key) or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            src, dst = str(item[0]), str(item[1])
            p = index.get((src, dst))
            if p is None:
                out.append(Assertion(src, dst, want, "未推算", "不在推算范围内（设备名写错，或该设备没有 IP）"))
            else:
                out.append(Assertion(src, dst, want, p.result, p.reason))
    return out


def _connected(adj: dict[str, dict]) -> bool:
    if not adj:
        return False
    start = next(iter(adj))
    seen = {start}
    q = deque([start])
    while q:
        cur = q.popleft()
        for nb in adj[cur]:
            if nb not in seen:
                seen.add(nb)
                q.append(nb)
    return len(seen) == len(adj)


def _path(adj: dict[str, dict], a: str, b: str) -> list[str] | None:
    """路由器图上的最短跳数路径（BFS，边不自带权重）。

    适用于按跳数选路的协议（RIP）。OSPF 要用 `_path_cost`。
    """
    if a == b:
        return [a]
    prev: dict[str, str | None] = {a: None}
    q = deque([a])
    while q:
        cur = q.popleft()
        for nb in adj.get(cur, {}):
            if nb in prev:
                continue
            prev[nb] = cur
            if nb == b:
                walk = [b]
                while prev[walk[-1]] is not None:
                    walk.append(prev[walk[-1]])
                return list(reversed(walk))
            q.append(nb)
    return None


def _path_cost(adj: dict[str, dict], a: str, b: str) -> list[str] | None:
    """按接口代价求最短路（Dijkstra）——OSPF 就是这么选路的。

    为什么不能直接用 BFS：实验 4-11 的拓扑里，两台路由器之间既有
    **1 跳的串口直连（代价 64）**，也有 **2 跳的以太绕行（代价 1+1=2）**。
    BFS 选 1 跳，OSPF 选 2 跳——结论正好相反。

    代价相同时依次比跳数、再比路径字符串，保证同一份输入每次算出同一条路
    （不然断言会时而通过时而不通过）。
    """
    if a == b:
        return [a]
    best: dict[str, tuple[int, int, list[str]]] = {a: (0, 0, [a])}
    heap: list[tuple[int, int, str, list[str]]] = [(0, 0, a, [a])]
    while heap:
        cost, hops, cur, path = heapq.heappop(heap)
        if cur == b:
            return path
        cur_best = best.get(cur)
        if cur_best is not None and (cost, hops) > cur_best[:2]:
            continue
        for nb in sorted(adj.get(cur, {})):
            my_if = adj[cur][nb][0]
            step = cost + _iface_cost(my_if)
            cand = (step, hops + 1)
            old = best.get(nb)
            if old is None or cand < old[:2]:
                best[nb] = (step, hops + 1, path + [nb])
                heapq.heappush(heap, (step, hops + 1, nb, path + [nb]))
    return None


def _path_for(topo: Topology, adj: dict[str, dict], a: str, b: str) -> list[str] | None:
    """按起点的路由协议选路：OSPF 看代价，其余按跳数。"""
    if _routing(topo, a).get("protocol") == "ospf":
        return _path_cost(adj, a, b)
    return _path(adj, a, b)


# ---------------------------------------------------------------- C2 路径断言
def _iface_ip(topo: Topology, dev: str, ifname: str) -> str | None:
    d = topo.device(dev)
    for itf in (d or {}).get("interfaces", []):
        if itf.get("name") == ifname and itf.get("ip"):
            return str(ipaddress.ip_interface(itf["ip"]).ip)
    return None


def _iface_area(topo: Topology, dev: str, ifname: str) -> int | None:
    d = topo.device(dev)
    for itf in (d or {}).get("interfaces", []):
        if itf.get("name") == ifname:
            return int(itf.get("area", 0))
    return None


def _iface_cost(ifname: str) -> int:
    """OSPF 接口开销的经验值：串口 64（T1 默认带宽 1544k）、以太 1。

    只用于「预期路由条目」里给出一个可对照的数字；PT 里以实际带宽为准，
    报告里已声明这一列是参考值。
    """
    low = ifname.lower()
    if low.startswith("serial"):
        return 64
    if low.startswith("gigabit"):
        return 1
    return 1


def _traces(topo: Topology, rep: Report, adj: dict[str, dict]) -> list[Trace]:
    """把每一对主机的推算路径翻译成 traceroute 期望跳序列。"""
    index = {h["name"]: h for h in _hosts(topo)}
    out: list[Trace] = []

    for p in rep.pairs:
        src, dst = index.get(p.src), index.get(p.dst)
        if src is None or dst is None:
            continue
        try:
            snet = str(ipaddress.ip_interface(src["ip"]).network)
            dnet = str(ipaddress.ip_interface(dst["ip"]).network)
        except ValueError:
            continue

        t = Trace(p.src, p.dst, p.dst_ip, routers=list(p.path))
        gw = src.get("gateway")

        # 同网段：tracert 只有一跳（目的主机自己）
        if snet == dnet:
            t.hops = [p.dst_ip]
            t.note = "同网段，二层直通，预期 1 跳"
            out.append(t)
            continue

        if not gw:
            t.result = "断"
            t.note = "源主机没配网关，第 1 跳就没有下一跳"
            out.append(t)
            continue

        # 包被某台路由器终止时，后面的路由器根本没收到包，跳序要截到那里
        path = list(p.path)
        if p.stop_at and p.stop_at in path:
            path = path[: path.index(p.stop_at) + 1]
        t.routers = path

        t.hops = [gw]
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            entry_if = adj.get(a, {}).get(b, (None, None))[1]
            ip = _iface_ip(topo, b, entry_if) if entry_if else None
            if ip:
                t.hops.append(ip)

        if p.ok:
            t.hops.append(p.dst_ip)
            t.note = f"预期 {len(t.hops)} 跳：{' → '.join(path) if path else '直连'}"
        else:
            t.result = "断"
            if "ACL" in p.reason:
                t.note = f"第 {len(t.hops)} 跳后 `*  *  *`（{p.stop_at} 上 ACL 静默丢弃，不回 ICMP）"
            elif "没有" in p.reason and "路由" in p.reason:
                t.note = f"第 {len(t.hops)} 跳 `!H`（{p.stop_at} 没有到 {dnet} 的路由，回 host unreachable）"
            elif "网关" in p.reason:
                t.note = p.reason
            else:
                t.note = f"{p.reason}；第 {len(t.hops)} 跳后超时"
        out.append(t)

    return out


_DISTANCE = {"rip": 120, "ospf": 110, "bgp": 20, "static": 1}
_CODE = {"rip": "R", "ospf": "O", "bgp": "B", "static": "S"}


def _route_asserts(
    topo: Topology,
    adj: dict[str, dict],
    nets: dict[str, list[tuple[str, str]]],
    igp_all: bool,
    graph_connected: bool,
) -> list[RouteAssert]:
    """每台路由器对每个「有主机的远端网段」应该出现的 show ip route 关键条目。

    C2 要求的「`show ip route` 关键条目断言」就是这张表：报告里给出预期行，
    提交前在 PT 里对着敲 `show ip route` 一眼就能核对。
    """
    host_nets = sorted({str(ipaddress.ip_interface(h["ip"]).network) for h in _hosts(topo)})
    out: list[RouteAssert] = []

    for r in _routers(topo):
        rt = _routing(topo, r)
        proto = rt.get("protocol") or "none"

        for net in host_nets:
            # 直连
            iface = _iface_facing(topo, r, net)
            if iface:
                out.append(RouteAssert(r, net, "C", f"C    {net} is directly connected, {iface}",
                                       True, "直连网段"))
                continue

            # 有类汇总：这条路由**学不到**（实验 4-10 的 v1 现象）
            if _rip_summary_blocks(topo, r, net):
                ver = rt.get("version", 2)
                major = _classful_cidr(net)
                out.append(RouteAssert(
                    r, net, "R", "（无）", False,
                    f"{r} 直连主网络 {major}，RIP v{ver} 按主网络通告、不带掩码，"
                    f"因此会丢弃从其他主网络接口学来的 {net} 路由"
                    f"（这是 v{ver} 有类协议的行为，不是配置写错）"
                ))
                continue

            owners = [n for n, _ in nets.get(net, []) if n in adj]
            if not owners:
                continue
            path = _path_for(topo, adj, r, owners[0])
            if path is None or len(path) < 2:
                out.append(RouteAssert(r, net, "?", "（无路径）", False,
                                       f"{r} 到 {net} 不存在路由器路径"))
                continue

            out_if = adj[r][path[1]][0]
            nh = _iface_ip(topo, path[1], adj[r][path[1]][1])

            # 静态路由优先看 DSL 里是否真的写了
            if proto in ("static", "none"):
                covered = False
                if rt.get("default_route"):
                    out.append(RouteAssert(r, net, "S", f"S*   0.0.0.0/0 [1/0] via {nh}", True,
                                           "默认路由覆盖，PT 里显示为 S* 候选默认路由"))
                    continue
                for route in rt.get("routes", []) or []:
                    try:
                        if ipaddress.ip_network(net).subnet_of(
                            ipaddress.ip_network(f"{route['network']}/{route['mask']}", strict=False)
                        ):
                            covered = True
                            ad = int(route.get("admin_distance") or 1)
                            out.append(RouteAssert(
                                r, net, "S", f"S    {net} [{ad}/0] via {nh}, {out_if}", True,
                                "静态路由显式覆盖"))
                            break
                    except (ValueError, KeyError):
                        continue
                if not covered:
                    out.append(RouteAssert(r, net, "S", "（无）", False,
                                           f"{r} 没跑 IGP，也没有覆盖 {net} 的静态路由"))
                continue

            if proto not in _DISTANCE:
                out.append(RouteAssert(r, net, "?", "（无）", False, f"{r} 的路由协议 {proto} 未被建模"))
                continue

            ok = igp_all and graph_connected
            cross_area = False
            shown_net = net
            sum_note = ""
            if proto == "rip":
                metric = len(path) - 1
                # RIP v1 只能自动汇总；v2 只有显式 no auto-summary 才不汇总。
                # 汇总后路由表里看到的是主网络，不是接口那个子网。
                ver = rt.get("version", 2)
                no_summary = (ver == 2 and rt.get("auto_summary") is False)
                if not no_summary:
                    cidr = _classful_cidr(net)
                    if cidr != net:
                        shown_net = cidr
                        sum_note = (
                            f"（RIP v{ver} 自动汇总：接口是 {net}，但通告不带掩码，"
                            f"路由表里显示主网络 {cidr}——这是 v{ver} 的固有行为，不是配置错）"
                        )
            elif proto == "ospf":
                metric = 0
                for i, hop in enumerate(path):
                    if i < len(path) - 1:
                        metric += _iface_cost(adj[hop][path[i + 1]][0])
                    else:
                        metric += _iface_cost(_iface_facing(topo, hop, net) or "FastEthernet0/0")
                # 出口所在区域 ≠ 目的网段所在区域 → 这条是跨区路由，PT 里显示 O IA
                out_area = _iface_area(topo, r, out_if)
                owner = path[-1]
                dst_if = _iface_facing(topo, owner, net)
                dst_area = _iface_area(topo, owner, dst_if) if dst_if else None
                cross_area = (
                    out_area is not None and dst_area is not None and out_area != dst_area
                )
            else:
                metric = 0

            code = _CODE[proto]
            if proto == "ospf" and cross_area:
                code = "O IA"
            ad = _DISTANCE[proto]
            entry = f"{code}    {shown_net} [{ad}/{metric}] via {nh}, 00:00:12, {out_if}"
            note = "IGP 已覆盖且路由器图连通，预期收敛" if ok else \
                   f"路由器图不连通或缺路由协议，{r} 可能学不到 {net}"
            if sum_note:
                note = note + sum_note if not ok else "IGP 已覆盖，预期收敛" + sum_note
            out.append(RouteAssert(r, net, code, entry, ok, note))

    return _merge_summarized(out)


def _merge_summarized(asserts: list[RouteAssert]) -> list[RouteAssert]:
    """把被 RIP 汇总压成同一条的多个断言并成一行。

    RIP v1（以及没关自动汇总的 v2）通告不带掩码，同一主网络下的 `/25` 与 `/26`
    在邻居路由表里都显示成 `192.168.16.0/24`，PT 的 `show ip route` 只出一行
    （代价相同则并列两个下一跳）。断言表如果照原样给两行，核对的人会以为少了一条。
    """
    merged: list[RouteAssert] = []
    seen: dict[tuple[str, str, str], RouteAssert] = {}
    for a in asserts:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+/\d+)\s+\[", a.entry)
        if m and m.group(1) != a.prefix:
            key = (a.device, a.code, m.group(1))
            prev = seen.get(key)
            if prev is not None:
                extra = (f"；{a.prefix} 汇总后同为 {m.group(1)}，度量相同 → "
                         f"PT 里并成这一行（可能并列两个下一跳）")
                prev.note = (prev.note + extra) if prev.note else extra
                continue
            seen[key] = a
        merged.append(a)
    return merged


def _ecmp_asserts(
    topo: Topology,
    adj: dict[str, dict],
    nets: dict[str, list[tuple[str, str]]],
) -> list[Ecmp]:
    """找出跑 OSPF 的路由器上「同一目的网段有两条等代价路径」的情况。

    这是实验 4-11 第 10 步（验证等价负载均衡）的验证点。手册的做法是
    在 Router2 上 `ping 30.0.0.2`，看 IP 数据报是否交替走两条路由。

    为什么这里会出现等价：R2 到 `30.0.0.0/30` 有两条路——
    经 R0（Fa2/0 代价 1 + R0 的 Se0/0 代价 64 = 65）与
    经 R1（Fa3/0 代价 1 + R1 的 Se0/0 代价 64 = 65）。
    两条都恰好「一段以太 + 一段串口」，代价自然相等。

    这正是之前「手册第 10 步没法验」的答案：不是手册写错，是我没把
    「同一目的、多个下一跳、代价相同」这件事算出来。
    """
    transit: dict[str, list[tuple[str, str]]] = {}
    for net, members in nets.items():
        owners = [(n, p) for n, p in members if n in adj]
        if len(owners) >= 2:  # 至少两台路由器接入 → 传输网段，不是主机网段
            transit[net] = owners

    out: list[Ecmp] = []
    for r in _routers(topo):
        if _routing(topo, r).get("protocol") != "ospf":
            continue
        for net in sorted(transit):
            owners = transit[net]
            if any(o == r for o, _ in owners):
                continue  # 自己直连，不是学习来的路由
            branches: list[tuple[int, str, str]] = []
            for owner, owner_if in owners:
                path = _path_cost(adj, r, owner)
                if path is None or len(path) < 2:
                    continue
                cost = 0
                for i in range(len(path) - 1):
                    cost += _iface_cost(adj[path[i]][path[i + 1]][0])
                cost += _iface_cost(owner_if)  # 最后一跳落在目的网段上的接口
                out_if = adj[r][path[1]][0]
                nh = _iface_ip(topo, path[1], adj[r][path[1]][1])
                branches.append((cost, nh or "?", out_if))
            if len(branches) < 2:
                continue
            best = min(b[0] for b in branches)
            winners = [b for b in branches if b[0] == best]
            if len(winners) < 2:
                continue
            out.append(Ecmp(
                device=r, prefix=net, cost=best,
                branches=[(nh, ifc) for _, nh, ifc in winners],
                note=("两条路径代价相同 → PT 的 `show ip route` 会并列两个下一跳，"
                      "流量按等价多路径轮流发；手册第 10 步就是看这个"),
            ))
    return out


# ---------------------------------------------------------------- 二层一致性
def _port_cfg(topo: Topology, dev: str, port: str) -> dict:
    d = topo.device(dev)
    if not d:
        return {}
    for itf in d.get("interfaces", []):
        if itf.get("name") == port:
            return itf
    return {}


def _neighbor(topo: Topology, dev: str, port: str) -> tuple[str, str] | None:
    """链路另一端是哪台设备的哪个口。"""
    me = f"{dev}:{port}"
    for ln in topo.links:
        a, b = ln["from"], ln["to"]
        if a == me:
            return tuple(b.split(":", 1))  # type: ignore[return-value]
        if b == me:
            return tuple(a.split(":", 1))  # type: ignore[return-value]
    return None


def _vlan_of_subnet(topo: Topology, net: str) -> int | None:
    for s in topo.data.get("subnets", []):
        try:
            if str(ipaddress.ip_network(s["cidr"], strict=False)) == net:
                return s.get("vlan")
        except ValueError:
            continue
    return None


def _l2_checks(topo: Topology) -> list[tuple[bool, str]]:
    """VLAN 场景下，主机接的交换机口、上行 trunk、路由器子接口三者必须配套。

    UC-05 早期就是这套链路没对上导致 ping 全丢，光看三层是发现不了的，
    所以单独查一遍：
      ① 主机所在的交换机 access 口 VLAN == 该网段声明的 VLAN
      ② 该交换机要有一个 trunk 口接路由器
      ③ 路由器那一侧要有对应 VLAN 的 dot1Q 子接口
    """
    checks: list[tuple[bool, str]] = []
    vlans_declared = {v["id"] for v in topo.data.get("vlans", [])}
    if not vlans_declared:
        return checks

    for h in _hosts(topo):
        net = str(ipaddress.ip_interface(h["ip"]).network)
        want = _vlan_of_subnet(topo, net)
        if want is None:
            continue
        nb = _neighbor(topo, h["name"], "FastEthernet0")
        if nb is None:
            checks.append((False, f"{h['name']} 没接到交换机上"))
            continue
        sw, sw_port = nb
        cfg = _port_cfg(topo, sw, sw_port)
        got = cfg.get("vlan")
        if cfg.get("mode") != "access" or got != want:
            checks.append(
                (False, f"{h['name']} 接在 {sw} {sw_port} 上，该口是 "
                        f"{cfg.get('mode', '未配置')}/VLAN {got if got is not None else '1'}，"
                        f"应为 access/VLAN {want}")
            )
        else:
            checks.append((True, f"{h['name']} → {sw} {sw_port} 是 access VLAN {want}"))

    # 每台交换机都要有 trunk 上行到路由器
    for d in topo.devices:
        if d["role"] != "switch":
            continue
        trunks = [i["name"] for i in d.get("interfaces", []) if i.get("mode") == "trunk"]
        if not trunks:
            if any(i.get("vlan") in vlans_declared for i in d.get("interfaces", [])):
                checks.append((False, f"{d['name']} 上划了业务 VLAN，但没有 trunk 口上联"))
            continue
        for t in trunks:
            nb = _neighbor(topo, d["name"], t)
            if nb is None:
                checks.append((False, f"{d['name']} {t} 是 trunk 但没连线"))
                continue
            rd, rp = nb
            sub = [
                i for i in (topo.device(rd) or {}).get("interfaces", [])
                if i.get("dot1q") in vlans_declared
            ]
            have = {i["dot1q"] for i in sub}
            missing = vlans_declared - have
            if missing:
                checks.append(
                    (False, f"{d['name']} {t} → {rd} {rp}，但 {rd} 缺 VLAN "
                            f"{'/'.join(str(v) for v in sorted(missing))} 的子接口")
                )
            else:
                checks.append((True, f"{d['name']} {t} trunk → {rd}，子接口齐备"))

    return checks


def _static_checks(topo: Topology, nets, adj, igp_all) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []

    # 纯二层场景（没有路由器、只有一段带主机的网段，如 UC-06 家庭工作室）：
    # 既不需要网关，也不需要三层接口。这两条检查在这里不适用，不能当成问题报出来。
    routers_all = _routers(topo)
    host_nets = {str(ipaddress.ip_interface(h["ip"]).network) for h in _hosts(topo)}
    l2_only = not routers_all and len(host_nets) <= 1
    if l2_only:
        checks.append((True, f"纯二层拓扑（{len(host_nets)} 个网段、无路由器），同网段直通不需要网关"))

    # 1. 每个主机都有网关，且网关是真实存在的路由器接口
    for h in _hosts(topo):
        if l2_only:
            break
        gw = h.get("gateway")
        if not gw:
            checks.append((False, f"{h['name']} 没有配网关，跨网段不通"))
        elif _gateway_router(topo, gw) is None:
            checks.append((False, f"{h['name']} 的网关 {gw} 不是任何路由器接口地址"))
        else:
            checks.append((True, f"{h['name']} 网关 {gw} 有效"))

    # 2. 每条点对点链路两端必须同网段
    for ln in topo.links:
        a = ln["from"].partition(":")[0]
        b = ln["to"].partition(":")[0]
        da, db = topo.device(a), topo.device(b)
        if not (da and db and da["role"] == "router" and db["role"] == "router"):
            continue
        na = {str(ipaddress.ip_interface(i["ip"]).network) for i in da.get("interfaces", []) if i.get("ip")}
        nb = {str(ipaddress.ip_interface(i["ip"]).network) for i in db.get("interfaces", []) if i.get("ip")}
        shared = na & nb
        checks.append(
            (bool(shared), f"{a}—{b} 两端网段{'一致：' + list(shared)[0] if shared else '不一致，链路起不来'}")
        )

    # 3. 每个声明的业务网段至少挂在一台路由器上（纯二层拓扑跳过）
    for s in ([] if l2_only else topo.data.get("subnets", [])):
        try:
            net = str(ipaddress.ip_network(s["cidr"], strict=False))
        except ValueError:
            continue
        has_l3 = any(n in adj for n, _ in nets.get(net, []))
        if not has_l3 and not s.get("vlan"):
            checks.append((False, f"网段 {net}（{s.get('name','')}）没有任何三层接口，无法路由"))

    # 4. 有 ACL 却没兜底规则
    for acl in topo.data.get("acls", []):
        rules = acl.get("rules", [])
        last = rules[-1] if rules else {}
        if last.get("action") != "permit":
            checks.append(
                (False, f"ACL {acl.get('id')} 最后一条不是 permit，未列出的流量会被隐含 deny 掉")
            )
        else:
            checks.append((True, f"ACL {acl.get('id')} 有 permit 兜底"))

    # 5. 路由协议覆盖性：跑了 IGP 但路由器图不连通
    routers = _routers(topo)
    if igp_all and not _connected(adj) and len(routers) > 1:
        checks.append((False, "路由器之间没有形成连通图，IGP 无法收敛成全网可达"))

    # 6. 二层配套（只有涉及 VLAN 的用例才产出）
    checks += _l2_checks(topo)

    return checks


# ---------------------------------------------------------------- 输出
def _matrix(rep: Report, hosts: list[str]) -> list[str]:
    idx = {(p.src, p.dst): p for p in rep.pairs}
    head = "| 源 ＼ 目的 | " + " | ".join(hosts) + " |"
    sep = "|---" * (len(hosts) + 1) + "|"
    lines = [head, sep]
    for s in hosts:
        cells = []
        for d in hosts:
            if s == d:
                cells.append("·")
                continue
            p = idx.get((s, d))
            cells.append("?" if p is None else ("通" if p.ok else "**×**"))
        lines.append(f"| **{s}** | " + " | ".join(cells) + " |")
    return lines


def to_markdown(rep: Report, topo: Topology) -> str:
    hosts = [h["name"] for h in _hosts(topo)]
    lines = [f"# {rep.uid} {rep.title} — 验收报告", ""]

    if rep.asserts:
        bad = len(rep.failed_asserts)
        verdict = "✅ 全部通过" if bad == 0 else f"❌ {bad} 条断言未通过"
    else:
        verdict = "（未声明 expect，只做一致性检查）" if not rep.failed_checks else "❌ 一致性检查未通过"
    lines += [f"**结论：{verdict}**", ""]

    lines += [
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 主机对 | {len(rep.pairs)} 组 |",
        f"| 推算通 / 不通 | {rep.n_reach} / {rep.n_block} |",
        f"| 断言 | {len(rep.asserts)} 条，通过 {len(rep.asserts) - len(rep.failed_asserts)} 条 |",
        f"| traceroute 路径断言 | {len(rep.traces)} 条，"
        f"预期可通 {sum(1 for t in rep.traces if t.ok)} 条 |",
        f"| 路由表断言 | {len(rep.routes)} 条，预期成立 "
        f"{sum(1 for r in rep.routes if r.ok)} 条 |",
        f"| 一致性检查 | {len(rep.checks)} 项，未通过 {len(rep.failed_checks)} 项 |",
    ]
    if rep.ecmps:
        lines.append(f"| 等价多路径 | {len(rep.ecmps)} 条（OSPF 等代价、多下一跳） |")
    lines += [
        "",
    ]

    lines += [
        "> **这张表是算出来的，不是 PT 跑出来的。** Packet Tracer 没有可脚本化的仿真接口，",
        "> 无法在命令行触发 ping，所以这里按拓扑模型推算预期结果：",
        "> 同网段按二层直通算；跨网段找路由器路径，且要求路径上每台路由器都有到目的网段的路由；",
        "> 路径上任何接口挂的 ACL 按顺序首条命中，末尾隐含 deny。",
        "> **提交时应在 PT 里按「预期通」「预期不通」各抽 2 条截图核对。**",
        "",
    ]

    if hosts:
        lines += ["## 连通性矩阵", "", "通 = 应能 ping 通；× = 应不通；· = 自身；? = 未推算", ""]
        lines += _matrix(rep, hosts)
        lines.append("")

    if rep.traces:
        lines += [
            "## traceroute 路径断言",
            "",
            "每行是**该在 PT 里看到的 `tracert` 结果**（`tracert 目的IP`）。"
            "跳序按「每台路由器接收包的接口地址」推，最后一行是目的主机。",
            "",
            "| 预期 | 源 → 目的 | 目的地址 | 跳数 | 期望跳序 | 依据 / 断点说明 |",
            "|---|---|---|---|---|---|",
        ]
        for t in rep.traces:
            mark = "✅" if t.ok else "❌ 应不通"
            hops = t.text()
            lines.append(
                f"| {mark} | {t.src} → {t.dst} | {t.dst_ip} | {len(t.hops)} | `{hops}` | {t.note} |"
            )
        lines.append("")

    if rep.asserts:
        lines += ["## 断言明细", "", "| 结果 | 源 | 目的 | 预期 | 推算 | 依据 |", "|---|---|---|---|---|---|"]
        for a in rep.asserts:
            mark = "✅" if a.ok else "❌"
            lines.append(f"| {mark} | {a.src} | {a.dst} | {a.want} | {a.got} | {a.reason} |")
        lines.append("")

    blocked = [p for p in rep.pairs if not p.ok]
    if blocked:
        lines += ["## 不通的原因", "", "| 源 | 目的 | 地址 | 原因 |", "|---|---|---|---|"]
        for p in blocked:
            lines.append(f"| {p.src} | {p.dst} | {p.src_ip} → {p.dst_ip} | {p.reason} |")
        lines.append("")

    if rep.routes:
        lines += [
            "## 路由表断言（`show ip route` 关键条目）",
            "",
            "在对应路由器上敲 `show ip route`，应当能看到下列条目。"
            "`[AD/度量]` 里的度量是**按接口带宽推算的参考值**，OSPF 实际值以设备为准；"
            "`via` 后面是下一跳地址。",
            "",
            "| 预期 | 路由器 | 目的前缀 | 类型 | 期望条目 | 说明 |",
            "|---|---|---|---|---|---|",
        ]
        for r in rep.routes:
            kind = {"C": "直连", "R": "RIP", "O": "OSPF", "O IA": "OSPF 跨区", "B": "BGP",
                    "S": "静态", "?": "未建模"}[r.code]
            mark = "✅" if r.ok else "⚠️"
            lines.append(
                f"| {mark} | {r.device} | {r.prefix} | {kind} | `{r.entry}` | {r.note} |"
            )
        lines.append("")

    if rep.ecmps:
        lines += [
            "## 等价多路径（ECMP）断言",
            "",
            "同一目的网段存在**两条代价相同**的 OSPF 路由时，PT 会并列两个下一跳，"
            "流量在两条路径间轮流发。这是 OSPF 等价负载均衡（ECMP）的判定依据——"
            "实验 4-11 第 10 步与附录 UC-02 都要求观察这个现象。",
            "",
            "| 路由器 | 目的前缀 | 共同代价 | 分支（下一跳 / 出口） | 怎么验 |",
            "|---|---|---|---|---|",
        ]
        for e in rep.ecmps:
            br = " ／ ".join(f"`{nh}` / `{ifc}`" for nh, ifc in e.branches)
            lines.append(
                f"| {e.device} | {e.prefix} | {e.cost} | {br} | "
                f"在该路由器上 `ping` 目的网段的某个地址，"
                f"看事件列表里两次 ping 的出口是否交替 |"
            )
        lines += [
            "",
            "跑 OSPF 时 `show ip route` 里这一条会显示成 "
            "`O    <前缀> [110/<共同代价>] via <下一跳1>, <出口1>`，再加一行同前缀、不同下一跳。",
            "",
        ]

    if rep.checks:
        lines += ["## 一致性检查", "", "| 结果 | 检查项 |", "|---|---|"]
        for ok, msg in rep.checks:
            lines.append(f"| {'√' if ok else '✗'} | {msg} |")
        lines.append("")

    if rep.failed_checks:
        lines += ["## 需要修的", ""]
        lines += [f"- {m}" for m in rep.failed_checks]
        lines.append("")

    return "\n".join(lines)

