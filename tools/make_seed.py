"""扩容骨架：从现有种子复制设备块，造出设备更多的新种子。

为什么需要：实验 8-1 的园区网要 5 路由 + 5 交换 + 8 主机 + 3 服务器 = 21 台，
而现有骨架最多 13 台（3 路由 + 3 交换 + 6 主机 + 1 电源设备）。而骨架是「只删不增」的——
生成器只会把骨架里的设备改名后拿去用，自己造不出新的设备块。

这里做的是**复制设备块**，但**复制不等于改名**——第一版只改了名字和 save-ref-id，
结果 PT 打不开文件。查清了原因：PT 载入 .pkt 时按一组**身份标识**建索引，
克隆时这些标识没换，多台设备共用同一个身份 → 索引冲突 → 拒绝加载。
一个设备的身份标识共 11 项，详见 ``pktlab/clone.py`` 的说明。

所以这里的做法是：
    ① 从种子里挑一个「样板」设备（按型号挑）
    ② 整块复制，给副本换上**全套新的**身份标识
       （save-ref-id / 设备地址 / 端口 MAC / IPv6 链路本地地址 / 物理视图 uuid / 序列号）
    ③ 在**物理视图**（机架图）里给副本注册一个节点
    ④ 副本的名字、坐标也改掉，避免和原设备撞名/重叠
    ⑤ 写回成新的种子文件

    python tools/make_seed.py --out seeds/base_campus.pkt \
        --routers 5 --switches 5 --pcs 8 --servers 3
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import clone, pkt  # noqa: E402

SRC = ROOT / "seeds" / "base_ospf_3r3sw6pc.pkt"

# 物理视图里的两个设备容器（照抄种子的结构）：
#   Rack            —— 路由器/交换机摆机架上，X 步长 7
#   Corporate Office —— PC 平铺，X 步长 86
RACK_X_BASE = 4
RACK_X_STEP = 7
OFFICE_X_BASE = 86
OFFICE_X_STEP = 86
OFFICE_Y = 215


def _model_of(block: str) -> str:
    m = re.search(r'<ENGINE>\s*<TYPE[^>]*model="([^"]*)"', block, re.S)
    return m.group(1) if m else ""


def _name_of(block: str) -> str:
    m = clone.NODE_NAME_RE.search(block)
    return m.group(1) if m else ""


def _serial_base(block: str) -> str:
    """序列号前缀：PC 是 PTT…，交换机是 CAT…。"""
    m = re.search(r"<SERIALNUMBER>([A-Z]{3})", block)
    return m.group(1) if m else "PTL"


def _find_container_children(raw: str, container: str) -> tuple[int, int]:
    """定位物理视图中某个容器节点下 ``<CHILDREN>`` 的**内容**区间。

    不能用正则 ``<CHILDREN>(.*?)</CHILDREN>``：容器是嵌套的，
    外层的 ``</CHILDREN>`` 会被内层的先匹配上，位置全错。
    改成从容器 NAME 出发，逐字符扫描并维护 CHILDREN 嵌套深度。
    """
    i = raw.find(f'<NAME translate="true">{container}</NAME>')
    if i < 0:
        return (-1, -1)
    k = raw.find("<CHILDREN>", i)
    if k < 0:
        return (-1, -1)
    start = k + len("<CHILDREN>")
    # 扫描 CHILDREN 的配对
    depth = 1
    pos = start
    while depth > 0:
        nxt_open = raw.find("<CHILDREN>", pos)
        nxt_close = raw.find("</CHILDREN>", pos)
        if nxt_close < 0:
            return (-1, -1)
        if nxt_open >= 0 and nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + len("<CHILDREN>")
        else:
            depth -= 1
            pos = nxt_close + len("</CHILDREN>")
            if depth == 0:
                return (start, nxt_close)
    return (-1, -1)


def _count_devices_in(raw: str, container: str) -> int:
    """容器下**直接**挂着多少台设备。

    只数直接子节点：容器是嵌套的，直接数 ``<NAME>`` 会把孙子也算进来
    （Corporate Office 下直接挂着 6 台 PC，但 Main Wiring Closet 里的
    机架和设备也会被数到）。

    做法：切出容器的 CHILDREN 内容，扫 ``<NODE>``/``</NODE>`` 维护深度，
    只取深度为 0 的那些 NODE 的名字。
    """
    s, e = _find_container_children(raw, container)
    if s < 0:
        return 0
    seg = raw[s:e]

    # 切出第一层 NODE 块
    top_blocks: list[str] = []
    i = 0
    while True:
        start = seg.find("<NODE>", i)
        if start < 0:
            break
        depth = 0
        j = start
        while True:
            m = re.compile(r"<NODE>|</NODE>").search(seg, j)
            if not m:
                break
            depth += 1 if m.group(0) == "<NODE>" else -1
            j = m.end()
            if depth == 0:
                break
        top_blocks.append(seg[start:j])
        i = j

    # 第一层里，名字是设备（而非子容器）的那些
    containers = {"Intercity", "Home City", "Corporate Office",
                  "Main Wiring Closet", "Rack"}
    count = 0
    for blk in top_blocks:
        m = re.search(r'<NAME translate="true">([^<]*)</NAME>', blk)
        if m and m.group(1) not in containers:
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--routers", type=int, default=0)
    ap.add_argument("--switches", type=int, default=0)
    ap.add_argument("--pcs", type=int, default=0)
    ap.add_argument("--servers", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20261022)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    src = Path(args.src)
    raw = pkt.read_pt_file(src).decode("utf-8")

    blocks = list(pkt.DEVICE_RE.finditer(raw))
    if not blocks:
        print("源种子里没有设备")
        return 1

    by_model: dict[str, list[str]] = {}
    for b in blocks:
        blk = b.group(0)
        by_model.setdefault(_model_of(blk), []).append(_name_of(blk))
    print("源种子设备：", {k: len(v) for k, v in by_model.items()})

    want = {
        "Router-PT-Empty": args.routers,
        "2960-24TT": args.switches,
        "PC-PT": args.pcs + args.servers,
    }

    # 身份标识分配器：先登记种子里已用的，保证新分配的都不撞
    pool = clone.IdPool()
    pool.observe(raw)

    # 物理视图两个容器里现有多少台设备 → 决定新设备的格子位置
    rack_n = _count_devices_in(raw, "Rack")
    office_n = _count_devices_in(raw, "Corporate Office")
    print(f"物理视图：Rack 下 {rack_n} 台，Corporate Office 下 {office_n} 台")

    extra: list[str] = []                            # 追加到 DEVICES 段的设备块
    new_nodes: dict[str, list[tuple[str, str, int, int]]] = {"Rack": [], "Corporate Office": []}

    seq = 0
    for model, need in want.items():
        have = by_model.get(model) or []
        if not have:
            print(f"[警告] 源种子里没有 {model}，跳过")
            continue
        template = have[0]
        tmpl_block = next(b.group(0) for b in blocks if _name_of(b.group(0)) == template)

        for _ in range(len(have), need):
            seq += 1
            new_name = f"{model.split('-')[0]}_x{seq}"

            if model == "PC-PT":
                office_n += 1
                container = "Corporate Office"
                pos = (OFFICE_X_BASE + (office_n - 1) * OFFICE_X_STEP, OFFICE_Y)
            else:
                rack_n += 1
                container = "Rack"
                pos = (RACK_X_BASE + (rack_n - 1) * RACK_X_STEP, 0)

            blk, new_uuid = clone.clone_device_block(
                tmpl_block,
                new_name,
                pool,
                canvas=pos,
                serial_base=_serial_base(tmpl_block),
            )
            extra.append(blk)
            new_nodes[container].append((new_name, new_uuid, pos[0], pos[1]))
            print(f"  复制 {template} → {new_name}  uuid={new_uuid[:13]}… pos={pos} 挂到 {container}")

    if not extra:
        print("不需要扩容")
        return 0

    # ---- 插入设备块到 DEVICES 段末尾 ----
    last = blocks[-1]
    raw = raw[: last.end()] + "".join(extra) + raw[last.end() :]

    # ---- 在物理视图里注册克隆设备 ----
    raw = _register_nodes(raw, new_nodes)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # .pkt 是加密容器（qCompress + Twofish-EAX），明文 XML 写进去 PT 打不开
    pkt.write_pt_file(out, raw.encode("utf-8"))
    total = len(blocks) + len(extra)
    print(f"已写出 {out}（新增 {len(extra)} 台设备，合计 {total} 台）")
    print("⚠️ 复制出来的设备块必须先在 PT 里打开这个种子确认能正常显示，"
          "再拿它去生成正式用例。")
    return 0


def _register_nodes(
    raw: str,
    new_nodes: dict[str, list[tuple[str, str, int, int]]],
) -> str:
    """把克隆设备挂进物理视图的 NODE 树。

    NODE 的 ``UUID_STR`` 必须与设备块 ``PHYSICAL`` 链末位一致 —— 这是 PT 的对账依据。
    缩进必须和同容器里已有的兄弟节点一致，否则 PT 解析层级会错位。
    """
    for container, items in new_nodes.items():
        if not items:
            continue
        s, e = _find_container_children(raw, container)
        if s < 0:
            print(f"[警告] 物理视图里找不到容器 {container}，{len(items)} 台设备未注册")
            continue
        indent = _child_indent(raw, s)
        body = "".join(
            clone.make_physical_node(name, uuid_str, x, y, indent=indent)
            for name, uuid_str, x, y in items
        )
        raw = raw[:s] + body + raw[s:]
    return raw


def _child_indent(raw: str, children_start: int) -> int:
    """容器 CHILDREN 内已有子节点的 ``<NODE>`` 缩进（没有则用 8）。"""
    m = re.compile(r"^([ ]*)<NODE>", re.M).search(raw, children_start)
    return len(m.group(1)) if m else 8


if __name__ == "__main__":
    raise SystemExit(main())
