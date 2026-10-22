"""拓扑图渲染：DSL → PNG / SVG。

不依赖 Graphviz，用 matplotlib 直接画出报告里能用的矢量图。
布局优先读 DSL 里的 pos，没有则按角色分层自动摆放。
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle
from matplotlib.colors import to_rgba

from . import layout
from .dsl import Topology

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

STYLE = {
    "router": {"color": "#378ADD", "fill": "#E6F1FB", "size": 0.34},
    "switch": {"color": "#1D9E75", "fill": "#E1F5EE", "size": 0.30},
    "pc": {"color": "#5F5E5A", "fill": "#F1EFE8", "size": 0.26},
    "server": {"color": "#BA7517", "fill": "#FAEEDA", "size": 0.30},
}

ROLE_ORDER = {"router": 0, "server": 1, "switch": 1, "pc": 2}

VLAN_COLORS = ["#378ADD", "#BA7517", "#1D9E75", "#A3479C", "#D4537E", "#0F8B8D"]


def _vlan_color(vid: int, order: list[int]) -> str:
    try:
        return VLAN_COLORS[order.index(vid) % len(VLAN_COLORS)]
    except ValueError:
        return "#888780"


def _host_vlan(topo: Topology, dev: dict) -> int | None:
    """主机属于哪个 VLAN：靠「主机地址落在哪个带 vlan 的子网里」反查。"""
    if not dev.get("ip"):
        return None
    try:
        net = ipaddress.ip_interface(dev["ip"]).network
    except ValueError:
        return None
    for s in topo.data.get("subnets", []):
        if not s.get("vlan"):
            continue
        try:
            if ipaddress.ip_network(s["cidr"], strict=False) == net:
                return s["vlan"]
        except ValueError:
            continue
    return None


def _auto_layout(topo: Topology) -> dict[str, tuple[float, float]]:
    layers: dict[int, list[str]] = {}
    for d in topo.devices:
        layers.setdefault(ROLE_ORDER.get(d["role"], 2), []).append(d["name"])
    pos: dict[str, tuple[float, float]] = {}
    for depth, names in layers.items():
        n = len(names)
        for i, name in enumerate(names):
            x = (i - (n - 1) / 2) * 2.2
            pos[name] = (x, -depth * 1.9)
    return pos


def _positions(topo: Topology) -> dict[str, tuple[float, float]]:
    """与 .pkt 用同一套布局，保证图上看到的就是 PT 里看到的。"""
    return layout.to_plot(layout.compute(topo))


def _draw_node(
    ax,
    name: str,
    role: str,
    x: float,
    y: float,
    subtitle: str,
    sub_dy: float = 0.0,
    sub_side: str = "bottom",
) -> None:
    st = STYLE.get(role, STYLE["pc"])
    s = st["size"]
    if role == "router":
        ax.add_patch(Circle((x, y), s, facecolor=st["fill"], edgecolor=st["color"], lw=1.6, zorder=3))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ax.annotate("", xy=(x + dx * s * 0.78, y + dy * s * 0.78),
                        xytext=(x + dx * s * 0.22, y + dy * s * 0.22),
                        arrowprops=dict(arrowstyle="-|>", color=st["color"], lw=1.1,
                                        shrinkA=0, shrinkB=0), zorder=4)
    elif role == "switch":
        ax.add_patch(
            FancyBboxPatch(
                (x - s, y - s * 0.42), 2 * s, s * 0.84,
                boxstyle="round,pad=0.02,rounding_size=0.06",
                facecolor=st["fill"], edgecolor=st["color"], lw=1.6, zorder=3,
            )
        )
    elif role == "server":
        ax.add_patch(
            Rectangle((x - s * 0.6, y - s), 2 * s * 0.6, 2 * s,
                      facecolor=st["fill"], edgecolor=st["color"], lw=1.6, zorder=3)
        )
    else:
        ax.add_patch(
            Rectangle((x - s * 0.9, y - s * 0.55), 2 * s * 0.9, 2 * s * 0.85,
                      facecolor=st["fill"], edgecolor=st["color"], lw=1.4, zorder=3)
        )
        ax.plot([x - s * 0.45, x + s * 0.45], [y - s * 0.85, y - s * 0.85],
                color=st["color"], lw=1.4, zorder=3)

    ax.text(x, y + s + 0.30, name, ha="center", va="bottom",
            fontsize=11, fontweight="bold", color="#26215C", zorder=5,
            bbox=dict(boxstyle="round,pad=0.10", fc="white", ec="none", alpha=0.86))
    if subtitle:
        box = dict(boxstyle="round,pad=0.16", fc="white", ec="none", alpha=0.88)
        if sub_side == "right":
            # 单臂路由的子接口地址挂在侧面，否则会压在 trunk 线和下层设备名上
            ax.text(x + s + 0.12, y, subtitle, ha="left", va="center",
                    fontsize=7.2, color="#5F5E5A", zorder=6, linespacing=1.35, bbox=box)
        else:
            ax.text(x, y - s - 0.26 + sub_dy, subtitle, ha="center", va="top",
                    fontsize=7.2, color="#5F5E5A", zorder=6, linespacing=1.35, bbox=box)


def render(
    topo: Topology,
    out_png: str | Path,
    out_svg: str | Path | None = None,
    show_links: bool = True,
) -> Path:
    pos = _positions(topo)

    # 画布尺寸按内容自适应：21 台设备挤在固定尺寸里必然压字。
    # 宽度随最宽的一层走，高度随层数走，但都留上下限。
    if pos:
        cols = [c for c, _ in pos.values()]
        rows = [r for _, r in pos.values()]
        span_x = max(cols) - min(cols) or 1
        span_y = max(rows) - min(rows) or 1
        # 内容本身的宽高比：图上要按这个比例出图，否则 aspect=equal 会把内容压扁
        # （21 台设备时纵向一压扁，整张图变成横条，标签全叠在一起）
        width = min(24.0, max(10.0, span_x * 1.05 + 3.0))
        height = min(15.0, max(6.0, span_y * 2.3 + 3.6))
    else:
        width, height = 10.6, 6.0
    fig, ax = plt.subplots(figsize=(width, height), dpi=200)

    host_vlan = {d["name"]: _host_vlan(topo, d) for d in topo.devices}
    vlan_order = sorted({v for v in host_vlan.values() if v})
    port_mode = {
        (d["name"], i["name"]): i["mode"]
        for d in topo.devices
        for i in d.get("interfaces", [])
        if i.get("mode")
    }

    # 同一排主机较多时，地址标注隔一台下沉，避免横向互相压字。
    # 下沉量按这一排的实际间距算：间距越小、标注越挤，就沉得越多。
    rows: dict[float, list[str]] = {}
    for d in topo.devices:
        if d["role"] in ("pc", "server") and d["name"] in pos:
            rows.setdefault(round(pos[d["name"]][1], 3), []).append(d["name"])
    stagger: dict[str, float] = {}
    for _, names in rows.items():
        if len(names) < 3:
            continue
        ordered = sorted(names, key=lambda k: pos[k][0])
        gaps = [pos[ordered[i + 1]][0] - pos[ordered[i]][0] for i in range(len(ordered) - 1)]
        gap = min(gaps) if gaps else 2.0
        # 标注宽约 3.4 个单位（"192.168.10.11" + "gw 192.168.10.254" 两行），
        # 间距不够时就下沉一格
        drop = 0.62 if gap >= 4.2 else 0.95
        for i, n in enumerate(ordered):
            if i % 2:
                stagger[n] = -drop

    if show_links:
        for ln in topo.links:
            a, _, a_port = ln["from"].partition(":")
            b, _, b_port = ln["to"].partition(":")
            if a not in pos or b not in pos:
                continue
            (x1, y1), (x2, y2) = pos[a], pos[b]
            serial = ln.get("cable") == "serial"
            is_trunk = port_mode.get((a, a_port)) == "trunk" or port_mode.get((b, b_port)) == "trunk"
            vid = host_vlan.get(a) or host_vlan.get(b)
            if is_trunk:
                color, lw = "#4A4A47", 3.2
            elif vid:
                color, lw = _vlan_color(vid, vlan_order), 1.8
            else:
                color, lw = "#888780", 1.8 if serial else 1.2
            ax.plot([x1, x2], [y1, y2], color=color, lw=lw, ls="-", zorder=1, solid_capstyle="round")
            if serial:
                ax.plot([x1, x2], [y1, y2], color="#E24B4A", lw=0.8, ls=(0, (3, 3)), zorder=2)
            label = ln.get("subnet")
            if label:
                ax.text((x1 + x2) / 2, (y1 + y2) / 2, label, ha="center", va="center",
                        fontsize=7.5, color="#185FA5", zorder=5,
                        bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="#D3D1C7", lw=0.5, alpha=1.0))

    # ---- VLAN 分组底色 ----
    # 按**行**分组画框：设备多的时候同一层会拆成几行，
    # 画一个大框会把不同行的设备全套进去，框线还压着别的 VLAN 的标签。
    if vlan_order:
        groups: dict[tuple[int, float], list[str]] = {}
        for name, vid in host_vlan.items():
            if vid and name in pos:
                key = (vid, round(pos[name][1], 3))
                groups.setdefault(key, []).append(name)
        for (vid, _row), names in sorted(groups.items(), key=lambda kv: -kv[0][1]):
            color = _vlan_color(vid, vlan_order)
            pts = [pos[n] for n in names]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x0, x1 = min(xs) - 0.30, max(xs) + 0.30
            # 单台设备独占一个框时，框要够宽容下它的地址标注，
            # 否则框线正好压在标注文字上
            if len(names) == 1:
                x0 -= 0.75
                x1 += 0.75
            # 框的下边界要低于该组里**最深**的标注（错行下沉的会低 0.95），
            # 否则框线正好压在标注文字上
            deepest = min((stagger.get(n, 0.0) for n in names), default=0.0)
            drop = 1.10
            if deepest < -0.5:
                drop += (abs(deepest) - 0.62) * 0.9
            y0, y1 = min(ys) - drop, max(ys) + 1.05
            ax.add_patch(
                FancyBboxPatch(
                    (x0, y0), x1 - x0, y1 - y0,
                    boxstyle="round,pad=0.02,rounding_size=0.12",
                    facecolor=to_rgba(color, 0.09), edgecolor=color,
                    lw=1.0, ls=(0, (4, 3)), zorder=0.6,
                )
            )
            # VLAN 标签放在框的**上边界外侧**：放框内会压住设备名，
            # 放顶部居中会被相邻 VLAN 的框线横穿，所以靠左放。
            ax.text(x0 + 0.10, y1 + 0.05, f"VLAN {vid}", ha="left", va="bottom",
                    fontsize=8.6, fontweight="bold", color=color, zorder=5,
                    bbox=dict(boxstyle="round,pad=0.16", fc="white", ec="none", alpha=0.85))

    # 同一排主机超过 3 台时，地址标注隔一台下沉半格，避免横向互相压字
    for d in topo.devices:
        x, y = pos[d["name"]]
        sub = d.get("ip", "")
        side = "bottom"
        if not sub and d.get("interfaces"):
            itfs = [i for i in d["interfaces"] if i.get("ip")]
            if any(i.get("dot1q") for i in itfs):
                # 单臂路由：子接口地址挂到节点右侧，竖排
                sub = "\n".join(i["ip"] for i in itfs[:3])
                side = "right"
            else:
                ips = [i["ip"] for i in itfs]
                sub = "  ".join(ips[:2]) + ("  …" if len(ips) > 3 else "")
        if d.get("gateway"):
            sub = f"{sub}\ngw {d['gateway']}" if sub else f"gw {d['gateway']}"
        _draw_node(ax, d["name"], d["role"], x, y, sub, stagger.get(d["name"], 0.0), side)

    title = topo.meta.get("title") or topo.meta.get("id", "")
    uid = topo.meta.get("id", "")
    ax.set_title(f"{uid}  {title}" if uid else title, fontsize=13, color="#2C2C2A", pad=14)

    ax.set_aspect("equal")
    ax.axis("off")
    ax.margins(0.18)
    with plt.rc_context({"font.size": 10}):
        fig.tight_layout()
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_png, bbox_inches="tight", facecolor="white")
        if out_svg:
            fig.savefig(out_svg, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png
