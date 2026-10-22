"""拓扑布局：把设备摆成好看的形状，同时给拓扑图和 .pkt 用同一套坐标。

返回的是抽象网格坐标 (col, row)，row 向下增大。调用方各自缩放：
  - 拓扑图：x = col, y = -row
  - Packet Tracer：x = 180 + col*155, y = 150 + row*150

规则
----
1. **按层次分行**：核心/汇聚/接入/终端各占一行，层次由「离网络核心的距离」定。
   之前是「路由器一行、交换机一行、主机一行」，遇到三层园区网（21 台设备）
   就会挤成两排、标签互相压字 —— 实验 8-1 的图就是这个问题。
2. **同层横向均分**：每层自己算列宽，设备多的层不会被别的层挤掉。
3. **同层设备保持左右顺序**：按它在拓扑里的位置排序，不乱跳。
4. 层内相邻设备的间距按该层设备数自适应，保证标签不重叠。
"""

from __future__ import annotations

from .dsl import Topology

# 层次名 → 排版层号（0 最靠上）。
# 归一化：DSL 里显式写的 layer 名五花八门（"核心层""汇聚层""接入层"…），
# 这里按关键字归到六个标准层次。
_LAYER_KEYS: list[tuple[str, int]] = [
    ("isp", 0),        # ISP / 上游
    ("边界", 1),        # 边界 / 出口
    ("出口", 1),
    ("border", 1),
    ("核心", 2),        # 核心 / 骨干
    ("core", 2),
    ("骨干", 2),
    ("汇聚", 3),        # 汇聚
    ("agg", 3),
    ("接入", 4),        # 接入
    ("edge", 4),
    ("终端", 5),        # 终端 / 主机
    ("host", 5),
]


def _layer_index(layer: str) -> int | None:
    """层次名 → 层号；认不出来返回 None。"""
    low = (layer or "").lower()
    for key, idx in _LAYER_KEYS:
        if key in low:
            return idx
    return None


def _neighbors(topo: Topology) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {d["name"]: set() for d in topo.devices}
    for ln in topo.links:
        a = ln["from"].partition(":")[0]
        b = ln["to"].partition(":")[0]
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _host_vlan(topo: Topology) -> dict[str, int]:
    """主机属于哪个 VLAN（供分组排序用）。"""
    import ipaddress

    out: dict[str, int] = {}
    vlan_subnets: dict[int, object] = {}
    for s in topo.data.get("subnets", []):
        if s.get("vlan"):
            try:
                vlan_subnets[s["vlan"]] = ipaddress.ip_network(s["cidr"], strict=False)
            except ValueError:
                pass
    for d in topo.devices:
        if d["role"] not in ("pc", "server") or not d.get("ip"):
            continue
        try:
            net = ipaddress.ip_interface(d["ip"]).network
        except ValueError:
            continue
        for vid, snet in vlan_subnets.items():
            if net == snet:
                out[d["name"]] = vid
                break
    return out


def _role_layer(role: str) -> int:
    """角色 → 基础层号。数字越小越靠上（越靠近核心）。"""
    return {"router": 0, "switch": 1, "pc": 2, "server": 2}.get(role, 2)


def compute(topo: Topology) -> dict[str, tuple[float, float]]:
    """返回 name -> (col, row)。

    分层有两条来源，**显式优先**：
      ① DSL 里写的 ``layer`` 字段 —— 层次本来就是设计决策，不该靠猜。
         园区网（实验 8-1）就是靠它画出「ISP → 出口 → 核心 → 汇聚 → 接入 → 终端」。
      ② 没有 layer 时，按「离终端的跳数」从下往上数（BFS），
         对普通拓扑（几台路由器一串）效果和原来一样。

    同层设备横向均分，列距随该层设备数自适应，保证标签不压字。
    """
    devices = {d["name"]: d for d in topo.devices}
    adj = _neighbors(topo)
    if not devices:
        return {}

    hosts = {n for n, d in devices.items() if d["role"] in ("pc", "server")}

    # ---- ① 定层 ----
    layer_of: dict[str, int] = {}
    for n, dev in devices.items():
        idx = _layer_index(dev.get("layer", ""))
        if idx is not None:
            layer_of[n] = idx

    # 没标 layer 的设备：按 BFS 跳数从终端往上数
    rest = [n for n in devices if n not in layer_of]
    if rest:
        depth: dict[str, int] = {}
        if hosts:
            frontier = set(hosts)
            d = 0
            while frontier:
                for n in frontier:
                    depth.setdefault(n, d)
                nxt = {nb for n in frontier for nb in adj[n] if nb not in depth}
                frontier = nxt
                d += 1
        # 已有层次的设备作为「锚点」，把 BFS 深度换算到它们的层号
        anchor_layers = sorted(set(layer_of.values()))
        base = anchor_layers[0] if anchor_layers else 0
        span = (anchor_layers[-1] - base) if anchor_layers else 1
        for n in rest:
            dev = devices[n]
            if dev["role"] in ("pc", "server"):
                # 终端永远在最下一层
                layer_of[n] = 5
                continue
            if n in depth:
                d = depth[n]
                if span >= 1:
                    layer_of[n] = base + round(span * (1 - min(d, span) / span))
                else:
                    layer_of[n] = base
            else:
                layer_of[n] = base

    # ---- ② 层内排序 ----
    vlan_of = _host_vlan(topo)

    def sort_key(n: str) -> tuple:
        dev = devices[n]
        return (_role_layer(dev["role"]), vlan_of.get(n, 0), n)

    by_layer: dict[int, list[str]] = {}
    for n, l in layer_of.items():
        by_layer.setdefault(l, []).append(n)
    for l in by_layer:
        by_layer[l] = sorted(by_layer[l], key=sort_key)

    # ---- ③ 每层单独算列宽并均分 ----
    def spread(names: list[str], row: int) -> None:
        n = len(names)
        step = 2.0 if n <= 4 else 2.0 + (n - 4) * 0.3
        start = -(n - 1) * step / 2
        for i, name in enumerate(names):
            pos[name] = (start + i * step, float(row))

    pos: dict[str, tuple[float, float]] = {}
    for l in sorted(by_layer):
        spread(by_layer[l], l)

    # ---- ④ 层号压缩成连续序号 ----
    # 上面的层号来自「层次语义」，可能跳号（比如没有汇聚层的拓扑）。
    # 跳号会让图里出现空白行，浪费画布，所以这里压成 0,1,2,…
    used = sorted(by_layer)
    remap = {l: i for i, l in enumerate(used)}
    for n in pos:
        pos[n] = (pos[n][0], float(remap[layer_of[n]]))
    by_layer = {remap[l]: v for l, v in by_layer.items()}

    # ---- ⑤ 层内顺序微调：让上下层之间的连线尽量不交叉 ----
    # 注意顺序：先按 VLAN 分组（同一 VLAN 的终端排在一起，图上好读），
    # 再按父设备位置微调。只按父设备位置排的话，VLAN 分组会被打散，
    # 图上同色的线七零八落。
    layers = sorted(by_layer)
    for i in range(1, len(layers)):
        upper = {name: pos[name][0] for name in by_layer[layers[i - 1]]}
        cur = by_layer[layers[i]]

        def neighbour_x(n: str, upper=upper) -> float:
            xs = [upper[nb] for nb in adj[n] if nb in upper]
            return sum(xs) / len(xs) if xs else pos[n][0]

        # 主机的排序键：先 VLAN，再父设备位置
        if any(devices[n]["role"] in ("pc", "server") for n in cur):
            cur.sort(key=lambda n: (vlan_of.get(n, 0), neighbour_x(n)))
        else:
            cur.sort(key=neighbour_x)
        spread(cur, layers[i])

    # ---- ⑥ 宽层拆行 ----
    # 一层里挤太多设备（比如 10 台 PC），横向就拉得特别长，
    # 整张图变成一条横带（aspect=equal 会把纵向压扁，标签全叠）。
    #
    # 拆行时**按 VLAN 连续切分**：层内已经按 VLAN 排好序了，
    # 所以同一个 VLAN 的设备是连续的，切开后同一个 VLAN 最多断成两段，
    # 不会散得到处都是。断行处优先落在 VLAN 边界上。
    MAX_PER_ROW = 6
    rows_out: dict[int, list[str]] = {}
    next_row = 0
    for l in layers:
        names = by_layer[l]
        if len(names) <= MAX_PER_ROW:
            rows_out[next_row] = names
            next_row += 1
            continue
        # 找切点：优先切在 VLAN 变化处，其次切在 MAX_PER_ROW 处
        chunks: list[list[str]] = []
        i = 0
        while i < len(names):
            limit = min(i + MAX_PER_ROW, len(names))
            # 从 limit 往前找最近的 VLAN 边界（但不小于 i+1）
            cut = limit
            for j in range(limit - 1, i, -1):
                if vlan_of.get(names[j], 0) != vlan_of.get(names[j - 1], 0):
                    cut = j
                    break
            if cut <= i:
                cut = limit
            chunks.append(names[i:cut])
            i = cut
        for chunk in chunks:
            rows_out[next_row] = chunk
            next_row += 1
    by_layer = rows_out
    # 重新铺一遍坐标（行号变了）
    for l in sorted(by_layer):
        spread(by_layer[l], l)

    return pos


# ---------- 两个消费者各自的坐标换算 ----------
def to_pt(pos: dict[str, tuple[float, float]]) -> dict[str, tuple[int, int]]:
    """Packet Tracer 画布坐标（左上原点，y 向下）。

    画布原点在左上角，负坐标会把设备扔到可视区外面。所以统一平移，
    保证所有设备都留出边距。
    """
    raw = {name: (200 + col * 155, 150 + row * 120) for name, (col, row) in pos.items()}
    if not raw:
        return {}
    dx = max(0, 70 - min(x for x, _ in raw.values()))
    dy = max(0, 70 - min(y for _, y in raw.values()))
    return {name: (int(x + dx), int(y + dy)) for name, (x, y) in raw.items()}


def to_plot(pos: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """报告插图坐标（数学坐标，y 向上）。

    ``compute`` 的层号已经是从上到下排的（0 最靠上），
    这里翻成数学坐标（y 越大越靠上）。

    横向按最宽的一层摊开，纵向给固定的层间距。**纵向间距要够大**：
    图里 ``aspect="equal"``，纵向一压扁，整张图就变成一条横带，
    设备名和地址全叠在一起（21 台设备时尤其明显）。
    """
    if not pos:
        return {}
    cols = [c for c, _ in pos.values()]
    rows = [r for _, r in pos.values()]
    span = max(cols) - min(cols)
    # 横向比例：层越宽摊得越开，但有上限，免得图变得横长条
    scale = 1.9 if span < 4 else (2.2 if span < 8 else 2.5)
    # 纵向层距固定 2.6（比横向 scale 大，保证行间不压）
    return {
        name: ((col - min(cols)) * scale, (max(rows) - row) * 3.4)
        for name, (col, row) in pos.items()
    }
