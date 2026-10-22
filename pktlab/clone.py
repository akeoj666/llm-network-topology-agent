"""设备块克隆：把一台设备复制成一台**真正独立**的新设备。

背景
----
实验 8-1 的园区网要 5 路由 + 5 交换 + 8 PC + 3 服务器 = 21 台设备，
而现成骨架最多 13 台。骨架是「只删不增」的——生成器只会把骨架里的设备
改名后拿去用，自己造不出新的设备块。所以只能复制。

第一版复制只改了名字和 save-ref-id，结果 PT **打不开**这个文件。
原因查清了：PT 载入 .pkt 时不是按设备名索引，而是按一组**身份标识**建索引。
克隆时这些标识没换，多台设备就共用同一个身份 → PT 载入时索引冲突 → 拒绝加载。

一个设备在 .pkt 里的身份标识（全部必须唯一）
--------------------------------------
========================= ====================================================
字段                       作用
========================= ====================================================
``SAVE_REF_ID``            设备主 id。链路里 ``<FROM>``/``<TO>`` 引用的就是它，
                           **必须带 ``save-ref-id:`` 前缀**（PT 写出来的就是这格式）
``MEM_ADDR``               设备内存地址（``WORKSPACE/LOGICAL`` 下）
``DEV_ADDR``               设备地址，链路的 ``*_DEVICE_MEM_ADDR`` 引用它
``MACADDRESS`` / ``BIA``   端口 MAC。跨设备不能重复，否则交换机会学错
``IPV6_LINK_LOCAL``        端口的 IPv6 链路本地地址，**由 MAC 推导**（EUI-64），
``IPV6_DEFAULT_LINK_LOCAL``所以改了 MAC 必须同步改这两个
``PHYSICAL`` 末位 uuid     设备在**物理视图**（机架图）里的节点 id，
                           必须与 ``PHYSICALWORKSPACE`` 里那个 NODE 的
                           ``UUID_STR`` 一致
``SERIALNUMBER``           序列号
``COORD_SETTINGS``         机架视图里的格子坐标
========================= ====================================================

物理视图注册
------------
``PHYSICALWORKSPACE`` 是一棵 NODE 树（Intercity → … → Rack → 各设备）。
每台设备在里面要有一个 NODE，``UUID_STR`` 与设备块 ``PHYSICAL`` 末位一致。
克隆设备如果不注册，PT 里物理视图就找不到它。

为什么 MAC 的 EUI-64 推导必须一起做
------------------------------------
PT 把 MAC 转成 IPv6 链路本地地址用的是 modified EUI-64：
把 MAC 的第一个字节的 U/L 位取反，中间接 ``ff:fe``，前面补 ``fe80::``。
例：``0001.9696.111D`` → ``FE80::201:96FF:FE96:111D``。
只改 MAC 不改这个字段，两边就对不上，PT 会认为地址非法。
"""

from __future__ import annotations

import re
import uuid as uuid_mod

# 设备块内各身份字段的正则
_SAVE_REF_RE = re.compile(r"<SAVE_REF_ID>([^<]*)</SAVE_REF_ID>")
_TEXT_REF_RE = re.compile(r"save-ref-id:(\d+)")
_MEM_RE = re.compile(r"<MEM_ADDR>([^<]*)</MEM_ADDR>")
_DEV_RE = re.compile(r"<DEV_ADDR>([^<]*)</DEV_ADDR>")
_MAC_RE = re.compile(r"<MACADDRESS>([^<]*)</MACADDRESS>")
_BIA_RE = re.compile(r"<BIA>([^<]*)</BIA>")
_LL_RE = re.compile(r"<IPV6_LINK_LOCAL>([^<]*)</IPV6_LINK_LOCAL>")
_DLL_RE = re.compile(r"<IPV6_DEFAULT_LINK_LOCAL>([^<]*)</IPV6_DEFAULT_LINK_LOCAL>")
_SERIAL_RE = re.compile(r"<SERIALNUMBER>([^<]*)</SERIALNUMBER>")
_PHYSICAL_RE = re.compile(r"<PHYSICAL>([^<]*)</PHYSICAL>")
_XY_RE = re.compile(r"(<LOGICAL>\s*<X>)([^<]*)(</X>\s*<Y>)([^<]*)(</Y>)", re.S)
_COORD_RE = re.compile(
    r"(<COORD_SETTINGS>\s*<X_COORD>)([^<]*)(</X_COORD>\s*<Y_COORD>)([^<]*)(</Y_COORD>)", re.S
)
_NODE_NAME_RE = re.compile(
    r'(<ENGINE>\s*<TYPE[^>]*>[^<]*</TYPE>\s*<NAME[^>]*>)([^<]*)(</NAME>)', re.S
)
# 对外暴露一个「取名字」的版本（名字在第 2 组）
NODE_NAME_RE = re.compile(
    r'<ENGINE>\s*<TYPE[^>]*>[^<]*</TYPE>\s*<NAME[^>]*>([^<]*)</NAME>', re.S
)

# 物理视图里放设备的两个机架：路由器/交换机在机架上，PC 在办公区平铺
RACK_X_STEP = 7      # 机架上每台设备占 7 格
OFFICE_X_STEP = 15   # 办公区每台设备占 15 格


def mac_to_link_local(mac: str) -> str:
    """MAC → IPv6 链路本地地址（modified EUI-64）。

    ``0001.9696.111D`` → ``FE80::201:96FF:FE96:111D``
    """
    hexs = mac.replace(".", "").replace(":", "").upper()
    if len(hexs) != 12:
        raise ValueError(f"MAC 地址格式不对: {mac}")
    raw = bytes.fromhex(hexs)
    # U/L 位（第一个字节的 bit1）取反
    first = raw[0] ^ 0x02
    eui = bytes([first]) + raw[1:3] + b"\xff\xfe" + raw[3:]
    # 压成 8 组十六进制
    groups = [eui[i : i + 2].hex().upper() for i in range(0, 8, 2)]
    # 去掉每组前导零（PT 的写法就是紧凑形式）
    groups = [g.lstrip("0") or "0" for g in groups]
    return "FE80::" + ":".join(groups)


def _bump_mac(mac: str, n: int) -> str:
    """把 MAC 的**后两字节**加上 n（保持前 4 位厂商 OUI 不变，避免撞别人的设备）。

    MAC 是 12 个十六进制字符：前 6 位是 OUI（厂商），后 6 位是设备序号。
    只动后 6 位里的低 4 位（16 bit 空间）足够一个种子用。
    输出保持基准 MAC 的分隔符风格。
    """
    hexs = mac.replace(":", "").replace(".", "").upper()
    if len(hexs) != 12:
        raise ValueError(f"MAC 地址格式不对: {mac}")
    head, tail = hexs[:6], hexs[6:]
    val = (int(tail, 16) + n) % (1 << 24)
    return _format_like(mac, f"{head}{val:06X}")


class IdPool:
    """全局身份分配器：保证产出文件里所有身份标识互不重复。"""

    def __init__(self) -> None:
        self.used_refs: set[str] = set()
        self.used_mems: set[str] = set()
        self.used_devs: set[str] = set()
        self.used_macs: set[str] = set()
        self.used_uuids: set[str] = set()
        self.used_serials: set[str] = set()
        self._counters: dict[str, int] = {}

    def observe(self, xml: str) -> None:
        """扫描既有内容，把已用标识登记进来（避免撞种子里的）。"""
        self.used_refs.update(_TEXT_REF_RE.findall(xml))
        self.used_mems.update(_MEM_RE.findall(xml))
        self.used_devs.update(_DEV_RE.findall(xml))
        self.used_macs.update(_MAC_RE.findall(xml))
        self.used_uuids.update(re.findall(r"<UUID_STR>([^<]*)</UUID_STR>", xml))
        self.used_serials.update(_SERIAL_RE.findall(xml))

    def _next(self, kind: str, start: int) -> int:
        n = self._counters.get(kind, start)
        self._counters[kind] = n + 1
        return n

    def new_ref(self) -> str:
        """新的 save-ref-id 数字部分。"""
        n = self._next("ref", 20_000_000_000_000_000_000)
        while str(n) in self.used_refs:
            n += 1
        self.used_refs.add(str(n))
        return str(n)

    def new_mem(self) -> str:
        """新的设备内存地址。用一个和种子不重叠的号段。"""
        n = self._next("mem", 1_500_000_000_000_000)
        while str(n) in self.used_mems:
            n += 1
        self.used_mems.add(str(n))
        return str(n)

    def new_dev(self) -> str:
        n = self._next("dev", 1_600_000_000_000_000)
        while str(n) in self.used_devs:
            n += 1
        self.used_devs.add(str(n))
        return str(n)

    def new_mac(self, base: str) -> str:
        """基于模板 MAC 造一个新的、保证全局唯一的 MAC。

        保持基准 MAC 的点分/冒号书写风格和厂商 OUI，只换设备序号。
        """
        n = self._next("mac", 0)
        bump = n + 1
        cand = _bump_mac(base, bump)
        while cand in self.used_macs:
            bump += 1
            cand = _bump_mac(base, bump)
        self.used_macs.add(cand)
        return cand

    def new_uuid(self) -> str:
        while True:
            u = "{" + str(uuid_mod.uuid4()) + "}"
            if u not in self.used_uuids:
                self.used_uuids.add(u)
                return u

    def new_serial(self, base: str) -> str:
        n = self._next("serial", 1)
        cand = f"{base}{n:04d}-"
        while cand in self.used_serials:
            n += 1
            cand = f"{base}{n:04d}-"
        self.used_serials.add(cand)
        return cand


def _rename(block: str, new_name: str) -> str:
    m = _NODE_NAME_RE.search(block)
    if not m:
        raise ValueError("设备块里找不到 NAME 节点")
    return block[: m.start(2)] + new_name + block[m.end(2) :]


def _format_like(base: str, hexs: str) -> str:
    """按基准 MAC 的分隔符风格输出新 MAC（种子用点分 ``000C.CFBD.7511``）。"""
    if "." in base:
        return f"{hexs[0:4]}.{hexs[4:8]}.{hexs[8:12]}"
    if ":" in base:
        return f"{hexs[0:2]}:{hexs[2:4]}:{hexs[4:6]}:{hexs[6:8]}:{hexs[8:10]}:{hexs[10:12]}"
    return hexs


def _replace_port_macs(block: str, pool: IdPool) -> str:
    """重分配端口 MAC，并同步 BIA 与两个 IPv6 链路本地地址。

    注意 BIA（烧录地址）必须和 MACADDRESS 一起改 —— PT 用 BIA 做 MAC 学习表
    的初值，两边不一致会出现「改了 IP 但 ping 不通」这类怪现象。

    映射是**整台设备**一份的：同一台设备里多个端口可能共用一个 MAC
    （种子里的路由器就是这样，三个口共用 000C.CFBD.7511）。
    克隆时要保持这种「共用」关系不变，只是整体换成新的、不会撞别人的值。
    """
    macs = _MAC_RE.findall(block)
    if not macs:
        return block

    # 整设备一份映射：老 MAC → 新 MAC
    mapping: dict[str, str] = {}
    for mac in macs:
        if mac not in mapping:
            mapping[mac] = pool.new_mac(mac)

    # ---- 第一步：先把 IPv6 链路本地地址按新 MAC 算好 ----
    # 顺序很关键：IPv6 链路本地地址是**从 MAC 推导**的（modified EUI-64），
    # 而 MAC 是用**旧值**查映射得到的。所以必须先算 LL、再换 MAC；
    # 反过来的话，换完 MAC 再查 mapping（旧值为键）就查不到了，
    # 结果 MAC 换了、LL 还是旧的 —— PT 会认为地址非法。
    def fix_ll(m: re.Match) -> str:
        seg = m.group(0)
        mm = _MAC_RE.search(seg)
        if not mm:
            return seg
        new = mapping.get(mm.group(1))
        if not new:
            return seg
        ll = mac_to_link_local(new)
        seg = _LL_RE.sub(f"<IPV6_LINK_LOCAL>{ll}</IPV6_LINK_LOCAL>", seg)
        seg = _DLL_RE.sub(f"<IPV6_DEFAULT_LINK_LOCAL>{ll}</IPV6_DEFAULT_LINK_LOCAL>", seg)
        return seg

    block = re.sub(r"<PORT>.*?</PORT>", fix_ll, block, flags=re.S)

    # ---- 第二步：换 MAC 与 BIA ----
    for old, new in mapping.items():
        block = block.replace(f"<MACADDRESS>{old}</MACADDRESS>", f"<MACADDRESS>{new}</MACADDRESS>")
        block = block.replace(f"<BIA>{old}</BIA>", f"<BIA>{new}</BIA>")

    return block


def clone_device_block(
    block: str,
    new_name: str,
    pool: IdPool,
    *,
    canvas: tuple[float, float] | None = None,
    serial_base: str | None = None,
) -> tuple[str, str]:
    """克隆一个设备块。

    返回 ``(新设备块, 新的物理视图 uuid)``。

    做的每一件事都对应上面表格里的一个身份字段 —— 少改一个，PT 就会拒绝加载。
    """
    # 1) 名字
    out = _rename(block, new_name)

    # 2) save-ref-id：换成新的，并**保留 save-ref-id: 前缀**
    #    （第一版克隆把前缀丢了，PT 认不出这就是一个 id）
    new_ref = pool.new_ref()
    out = _TEXT_REF_RE.sub(f"save-ref-id:{new_ref}", out)
    out = _SAVE_REF_RE.sub(f"<SAVE_REF_ID>save-ref-id:{new_ref}</SAVE_REF_ID>", out, count=1)

    # 3) 设备内存地址 / 设备地址
    out = _MEM_RE.sub(f"<MEM_ADDR>{pool.new_mem()}</MEM_ADDR>", out, count=1)
    out = _DEV_RE.sub(f"<DEV_ADDR>{pool.new_dev()}</DEV_ADDR>", out, count=1)

    # 4) 端口 MAC（含 BIA 与 IPv6 链路本地地址）
    out = _replace_port_macs(out, pool)

    # 5) 序列号
    if serial_base:
        out = _SERIAL_RE.sub(f"<SERIALNUMBER>{pool.new_serial(serial_base)}</SERIALNUMBER>", out, count=1)

    # 6) 物理视图 uuid：PHYSICAL 链的**末位**换成新 uuid
    new_uuid = pool.new_uuid()
    m = _PHYSICAL_RE.search(out)
    if m:
        items = [x.strip() for x in m.group(1).split(",")]
        if items:
            items[-1] = new_uuid
            out = out[: m.start(1)] + ",".join(items) + out[m.end(1) :]

    # 7) 画布坐标（PT 逻辑视图里的位置）
    if canvas:
        x, y = canvas
        out = _XY_RE.sub(
            lambda mm: f"{mm.group(1)}{x:g}{mm.group(3)}{y:g}{mm.group(5)}", out, count=1
        )

    # 8) 机架坐标（物理视图里占的格子）
    if canvas:
        x, y = canvas
        out = _COORD_RE.sub(
            lambda mm: f"{mm.group(1)}{x:g}{mm.group(3)}{y:g}{mm.group(5)}", out, count=1
        )

    return out, new_uuid


def make_physical_node(
    name: str,
    uuid_str: str,
    x: int,
    y: int,
    slot: int = 0,
    indent: int = 6,
) -> str:
    """生成物理视图里的一个设备 NODE（结构照抄 PT 原生写法）。

    ``indent`` 是 ``<NODE>`` 的缩进空格数。PT 原生文件里：
    容器自身的 CHILDREN 内首层节点缩进 8 格、再下一层 6 格。
    缩进要和同容器里的兄弟节点一致，否则 PT 解析层级会错位
    （表现为设备的 uuid 归属到容器头上）。
    """
    pad = " " * indent
    inner = " " * (indent + 1)
    return f"""{pad}<NODE>
{inner}<X>{x}</X>
{inner}<Y>{y}</Y>
{inner}<TYPE>6</TYPE>
{inner}<NAME translate="true">{name}</NAME>
{inner}<SX>0.2</SX>
{inner}<SY>0.2</SY>
{inner}<W>20</W>
{inner}<H>20</H>
{inner}<D>0</D>
{inner}<PATH isanim="false">../art/Background/grid_100x100.png</PATH>
{inner}<CHILDREN/>
{inner}<MANUAL_SCALING>false</MANUAL_SCALING>
{inner}<SCALED_PIXMAP_WIDTH>0</SCALED_PIXMAP_WIDTH>
{inner}<SCALED_PIXMAP_HEIGHT>0</SCALED_PIXMAP_HEIGHT>
{inner}<INIT_WIDTH>20</INIT_WIDTH>
{inner}<INIT_HEIGHT>20</INIT_HEIGHT>
{inner}<INIT_DEPTH>0</INIT_DEPTH>
{inner}<INIT_SX>0.2</INIT_SX>
{inner}<INIT_SY>0.2</INIT_SY>
{inner}<INIT_SZ>0.2</INIT_SZ>
{inner}<BG_TILED>false</BG_TILED>
{inner}<CUSTOM_IMAGE_WIDTH>-1</CUSTOM_IMAGE_WIDTH>
{inner}<CUSTOM_IMAGE_HEIGHT>-1</CUSTOM_IMAGE_HEIGHT>
{inner}<SCALE_FACTOR>1</SCALE_FACTOR>
{inner}<UUID_STR>{uuid_str}</UUID_STR>
{inner}<SLOT>{slot}</SLOT>
{inner}<SUB_SLOT>0</SUB_SLOT>
{pad}</NODE>
"""
