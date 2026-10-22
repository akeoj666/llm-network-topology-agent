"""拓扑布局：把设备摆成好看的形状，同时给拓扑图和 .pkt 用同一套坐标。

返回的是抽象网格坐标 (col, row)，row 向下增大。调用方各自缩放：
  - 拓扑图：x = col, y = -row
  - Packet Tracer：x = 180 + col*155, y = 150 + row*150

规则：
  1. 路由器排在最上排。如果路由器之间存在环（如三台全互联），改用弧形摆放，
     避免三台处在同一条直线上导致直线链路互相压住。
  2. 交换机排第二排，横向对齐它所连接的路由器。
  3. PC / 服务器排第三排，挂在所属交换机（或路由器）下方。
"""

from __future__ import annotations

from .dsl import Topology


def _neighbors(topo: Topology) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {d["name"]: set() for d in topo.devices}
    for ln in topo.links:
        a = ln["from"].partition(":")[0]
        b = ln["to"].partition(":")[0]
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _has_cycle(nodes: list[str], adj: dict[str, set[str]]) -> bool:
    """在给定子图上判断是否存在环（并查集）。"""
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in nodes:
        for b in adj[a]:
            if b not in parent or a >= b:
                continue
            ra, rb = find(a), find(b)
            if ra == rb:
                return True
            parent[ra] = rb
    return False


def _order_by_bfs(nodes: list[str], adj: dict[str, set[str]], start: str) -> list[str]:
    seen = {start}
    order = [start]
    queue = [start]
    while queue:
        cur = queue.pop(0)
        for nb in sorted(adj[cur]):
            if nb in nodes and nb not in seen:
                seen.add(nb)
                order.append(nb)
                queue.append(nb)
    for n in nodes:
        if n not in seen:
            order.append(n)
    return order


def compute(topo: Topology) -> dict[str, tuple[float, float]]:
    """返回 name -> (col, row)。"""
    devices = {d["name"]: d for d in topo.devices}
    adj = _neighbors(topo)

    routers = [n for n, d in devices.items() if d["role"] == "router"]
    switches = [n for n, d in devices.items() if d["role"] == "switch"]
    # 服务器和 PC 一样是末端设备，跟着它所属的交换机走，
    # 单独排一列会让「交换机→服务器」这条线斜穿整个图。
    hosts = [n for n, d in devices.items() if d["role"] in ("pc", "server")]

    pos: dict[str, tuple[float, float]] = {}

    # ---------- 路由器 ----------
    if routers:
        start = max(routers, key=lambda n: len([x for x in adj[n] if x in routers]))
        routers = _order_by_bfs(routers, adj, start)
    cyclic = _has_cycle(routers, adj) if len(routers) >= 3 else False
    n_r = len(routers)

    # 树状 / 链状没有环，但 BFS 会把枢纽路由器排到最边上，
    # 于是「枢纽—远端」这条边就从中间那台设备身上穿过去。
    # 把度数最大的那台挪到正中间，它的链路就都落在相邻列上了。
    if not cyclic and n_r >= 3:
        deg = {n: len([x for x in adj[n] if x in routers]) for n in routers}
        hub = max(routers, key=lambda n: deg[n])
        if deg[hub] >= 2:
            k = (n_r - 1) // 2
            rest = [n for n in routers if n != hub]
            routers = rest[:k] + [hub] + rest[k:]

    for i, name in enumerate(routers):
        if n_r == 1:
            pos[name] = (0.0, 0.0)
        elif cyclic:
            # 有环时奇数位下沉一整行，形成三角形，避免三点共线导致链路互相压住
            pos[name] = (i * 2.0, 1.0 if i % 2 else 0.0)
        else:
            pos[name] = (i * 2.0, 0.0)

    # ---------- 服务器 ----------
    row_after_routers = max((p[1] for p in pos.values()), default=0.0) + 1.0

    # ---------- 交换机 ----------
    # 一台路由器下可能挂多台交换机（比如一台接办公网、一台上服务器区），
    # 要按数量平均摊开，否则后一台会压在前一台上。
    by_parent: dict[str, list[str]] = {}
    lone: list[str] = []
    for name in switches:
        parents = [n for n in adj[name] if n in routers]
        if parents:
            by_parent.setdefault(parents[0], []).append(name)
        else:
            lone.append(name)

    for parent, group in by_parent.items():
        base = pos[parent][0]
        for i, name in enumerate(group):
            offset = (i - (len(group) - 1) / 2) * 0.95
            pos[name] = (base + offset, row_after_routers)

    for name in lone:
        col = max((p[0] for p in pos.values()), default=0.0) + 2.0
        pos[name] = (col, row_after_routers)

    # ---------- 主机 ----------
    for name in hosts:
        parents = [n for n in adj[name] if n in switches] or [n for n in adj[name] if n in routers]
        if parents:
            pcol, prow = pos.get(parents[0], (0.0, row_after_routers))
            siblings = [h for h in hosts if parents[0] in adj[h]]
            k = siblings.index(name) if name in siblings else 0
            offset = (k - (len(siblings) - 1) / 2) * 0.62
            pos[name] = (pcol + offset, prow + 1.0)
        else:
            col = max((p[0] for p in pos.values()), default=0.0) + 2.0
            pos[name] = (col, 2.5)

    # 兜底：没排到的设备统一放最后一排
    for name in devices:
        if name not in pos:
            col = max((p[0] for p in pos.values()), default=0.0) + 2.0
            pos[name] = (col, 2.5)
    return pos


# ---------- 两个消费者各自的坐标换算 ----------
def to_pt(pos: dict[str, tuple[float, float]]) -> dict[str, tuple[int, int]]:
    """Packet Tracer 画布坐标（左上原点，y 向下）。

    画布原点在左上角，负坐标会把设备扔到可视区外面（UC-05 六台 PC 横排时
    最左边那台就会落到 x=-40）。所以统一平移，保证所有设备都留出边距。
    """
    raw = {name: (200 + col * 155, 150 + row * 120) for name, (col, row) in pos.items()}
    if not raw:
        return {}
    dx = max(0, 70 - min(x for x, _ in raw.values()))
    dy = max(0, 70 - min(y for _, y in raw.values()))
    return {name: (int(x + dx), int(y + dy)) for name, (x, y) in raw.items()}


def to_plot(pos: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """报告插图坐标（数学坐标，y 向上）。"""
    return {name: (col * 1.6, -row * 1.6) for name, (col, row) in pos.items()}
