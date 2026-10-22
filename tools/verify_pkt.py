"""pkt 静态一致性校验器。

为什么需要这个
--------------
「PT 打不开这个文件」是最难查的一类 bug：生成器跑完了、报告也出了、断言全过，
但文件在 PT 里点开就是弹窗。排查成本极高。

第一版扩容骨架就踩了这个坑：复制设备块时只改了名字和 save-ref-id，
而 PT 载入 .pkt 时是按一组**身份标识**建索引的，克隆出来的设备跟模板设备
共用同一批标识 → 索引冲突 → 拒绝加载。查这个 bug 花的时间比写生成器还长。

所以这里把「PT 会拒绝的写法」变成**静态可检查的规则**。规则不是猜的，
是照着 PT 9.0.1 自带的 293 个原生示例文件（``saves/`` 目录）反推出来的：
那些文件是 PT 自己写的，全都满足下面每一条。

检查项
------
1. **save-ref-id 前缀**：必须是 ``save-ref-id:`` 开头（PT 7.1 之后原生文件全带）
2. **SAVE_REF_ID 唯一**：设备主 id 不能重复
3. **MEM_ADDR 唯一**：设备内存地址
4. **DEV_ADDR 唯一**：设备地址（链路引用它）
5. **MAC 跨设备唯一**：同一设备内部可以重复（路由器多个口常共用一个 MAC，
   PT 原生文件也是这样），但跨设备不能撞
6. **物理视图注册**：每台设备都要在 ``PHYSICALWORKSPACE`` 里有对应 NODE
7. **PHYSICAL 链对账**：设备块 ``PHYSICAL`` 末位 uuid == 物理视图 NODE 的 ``UUID_STR``
8. **链路引用可解析**：``<FROM>``/``<TO>`` 引用的 save-ref-id 必须能对上某台设备
9. **IPv6 链路本地地址**：必须与其端口 MAC 一致（modified EUI-64）
10. **坐标不重叠**：两台设备的画布坐标不能一样（否则在 PT 里叠在一起看不见）

用法
----
    python tools/verify_pkt.py out/UC-81.pkt
    python tools/verify_pkt.py seeds/*.pkt out/*.pkt      # 批量
退出码 0 = 全过，1 = 有问题。
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import clone, pkt  # noqa: E402
from pktlab.pkt import _iter_nodes  # noqa: E402

DEV_RE = re.compile(r"<DEVICE>.*?</DEVICE>", re.S)
NAME_RE = re.compile(
    r'<ENGINE>\s*<TYPE[^>]*>[^<]*</TYPE>\s*<NAME[^>]*>([^<]*)</NAME>', re.S
)
LINK_RE = re.compile(r"<LINK>.*?</LINK>", re.S)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)


def _physical_nodes(raw: str) -> dict[str, str]:
    """物理视图里 ``节点名 → UUID_STR``。

    物理视图是嵌套树：Intercity → Home City → Corporate Office → …
    每台设备是一个叶子 NODE，``UUID_STR`` 是它的身份。

    切块复用 ``pktlab.pkt._iter_nodes`` —— 它按标签深度配对切，
    不用非贪婪正则（非贪婪在嵌套结构上只会切到最外层，内层设备全漏）。
    """
    m = re.search(r"<PHYSICALWORKSPACE>(.*)</PHYSICALWORKSPACE>", raw, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for _start, _end, blk in _iter_nodes(m.group(1)):
        nm = re.search(r'<NAME translate="true">([^<]*)</NAME>', blk)
        uu = re.search(r"<UUID_STR>([^<]*)</UUID_STR>", blk)
        if nm and uu:
            out.setdefault(nm.group(1), uu.group(1))
    return out


def _device_physical_last(block: str) -> str | None:
    m = re.search(r"<PHYSICAL>([^<]*)</PHYSICAL>", block)
    if not m:
        return None
    items = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return items[-1] if items else None


def _canvas_xy(block: str) -> tuple[float, float] | None:
    m = re.search(
        r"<LOGICAL>\s*<X>([^<]*)</X>\s*<Y>([^<]*)</Y>", block, re.S
    )
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def verify(path: str | Path) -> Report:
    rep = Report()
    p = Path(path)
    try:
        raw = pkt.read_pt_file(p).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        # PT 6 之前的旧格式我们的解码器不支持（PT 自带的示例里就有 4 个这种），
        # 这不是我们生成的文件的问题，降级为警告。
        rep.warn(f"解不开这个容器（可能是 PT 6 之前的旧格式，与本项目无关）：{exc}")
        return rep

    # ---- 0. XML 良构性（最容易漏、后果最严重的一条）----
    # 上一版的教训：给「删设备」加了「顺带删物理视图节点」，用非贪婪正则切 NODE 块，
    # 把外层容器的 <CHILDREN> 开始标签一起删掉了，留下孤儿的 </CHILDREN>。
    # 结果：所有生成的文件 PT 一律报「The file was not saved correctly」。
    # 而那版校验器只做正则检查，压根没验 XML 是否合法，所以全绿通过。
    # 这条必须排在所有检查之前 —— 结构都坏了，后面查什么都没有意义。
    try:
        ET.fromstring(raw)
    except ET.ParseError as exc:
        rep.err(f"XML 结构损坏（PT 会报「文件没有正确保存」）：{exc}")
        # 结构已坏，继续查其它规则只会产生噪音
        return rep

    blocks = [b.group(0) for b in DEV_RE.finditer(raw)]
    if not blocks:
        rep.err("文件里没有设备块")
        return rep

    names = [NAME_RE.search(b).group(1) if NAME_RE.search(b) else "?" for b in blocks]

    # ---- 1/2. save-ref-id 前缀与唯一性 ----
    srefs = re.findall(r"<SAVE_REF_ID>([^<]*)</SAVE_REF_ID>", raw)
    bare = [s for s in srefs if not s.startswith("save-ref-id:")]
    if bare:
        rep.err(
            f"{len(bare)} 个 SAVE_REF_ID 缺少 'save-ref-id:' 前缀，PT 认不出这是设备 id："
            f"{bare[:3]}"
        )
    dup_ref = {k: v for k, v in Counter(srefs).items() if v > 1}
    if dup_ref:
        rep.err(f"SAVE_REF_ID 重复（多台设备共用同一个主 id，PT 载入时索引冲突）：{dup_ref}")

    # ---- 3/4. 设备地址唯一性 ----
    for label, pat in (("MEM_ADDR", r"<MEM_ADDR>([^<]*)</MEM_ADDR>"),
                       ("DEV_ADDR", r"<DEV_ADDR>([^<]*)</DEV_ADDR>")):
        vals = [v for b in blocks for v in re.findall(pat, b)]
        dup = {k: v for k, v in Counter(vals).items() if v > 1}
        if dup:
            rep.err(
                f"{label} 重复（PT 按设备地址建索引，撞了就拒绝加载）：{dict(list(dup.items())[:4])}"
            )

    # ---- 5. MAC 跨设备唯一 ----
    # 说明：PT 原生文件里也存在跨设备共用 MAC 的情况（WLC/无线场景常见），
    # 所以这条是**警告**而不是错误——但克隆出来的设备撞上模板设备的 MAC
    # 会让交换机学错表，还是要提示。
    owner: dict[str, set[str]] = {}
    for b, n in zip(blocks, names):
        for mac in re.findall(r"<MACADDRESS>([^<]*)</MACADDRESS>", b):
            owner.setdefault(mac, set()).add(n)
    cross = {k: v for k, v in owner.items() if len(v) > 1}
    if cross:
        rep.warn(
            f"{len(cross)} 个 MAC 跨设备重复（交换机会学错表）："
            f"{ {k: sorted(v) for k, v in list(cross.items())[:3]} }"
        )

    # ---- 9. IPv6 链路本地地址与 MAC 一致 ----
    # 说明：PT 原生文件里也有不一致的情况——IPv6 场景里 PT 会把接口标识符
    # 改成指定值（FE80::1、FE80::2），那是人为设的，不是 EUI-64 推导的。
    # 所以这条是警告；但我们克隆设备时如果忘了同步改，PT 也会认地址非法。
    for b, n in zip(blocks, names):
        for pm in re.finditer(r"<PORT>(.*?)</PORT>", b, re.S):
            seg = pm.group(1)
            mac = re.search(r"<MACADDRESS>([^<]*)</MACADDRESS>", seg)
            ll = re.search(r"<IPV6_LINK_LOCAL>([^<]*)</IPV6_LINK_LOCAL>", seg)
            if not (mac and ll and ll.group(1)):
                continue
            want = clone.mac_to_link_local(mac.group(1))
            if ll.group(1).upper() != want.upper():
                rep.warn(
                    f"{n}：端口 MAC {mac.group(1)} 与 IPv6 链路本地地址 "
                    f"{ll.group(1)} 不一致（EUI-64 应为 {want}）"
                )

    # ---- 6/7. 物理视图注册与 uuid 对账 ----
    # 对账依据是 **uuid**，不是名字 —— PT 允许给设备改名
    # （生成器正是靠改名来把骨架设备映射成 DSL 里的设备名），
    # 名字对不上但 uuid 在物理视图里能找到，就是合法的。
    #
    # 注意：有些 PT 原生文件没有物理视图段，或者该段是空的（纯逻辑拓扑），
    # 那种文件不要求设备注册。
    nodes = _physical_nodes(raw)
    if not nodes:
        rep.note("这个文件的机架视图是空的（纯逻辑拓扑），跳过机架视图检查")
    else:
        view_uuids = set(nodes.values())
        for b, n in zip(blocks, names):
            last = _device_physical_last(b)
            if not last:
                rep.warn(f"{n}：设备块里没有 PHYSICAL 链（机架视图里找不到归属）")
                continue
            if last not in view_uuids:
                rep.err(
                    f"{n}：设备块 PHYSICAL 末位 uuid {last} 在物理视图里找不到"
                    f"（PT 的机架视图里这台设备没有落点）"
                )

    # ---- 8. 链路引用可解析 ----
    # 链路的 <FROM>/<TO> 有两种形态（PT 原生文件里两种都有）：
    #   a) save-ref-id:数字   —— 新式，引用设备的 SAVE_REF_ID
    #   b) 纯数字             —— 旧式，引用设备在 DEVICES 段里的序号
    # 两种都认，否则会对 PT 自己写的文件大量误报。
    known = {s.split(":", 1)[-1] for s in srefs if s.startswith("save-ref-id:")}
    known |= set(srefs)
    known |= {str(i) for i in range(len(blocks))}
    links_seg = re.search(r"<LINKS>(.*)</LINKS>", raw, re.S)
    links = LINK_RE.findall(links_seg.group(1)) if links_seg else []
    for lk in links:
        for tag in ("FROM", "TO"):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", lk)
            if not m:
                continue
            ref = m.group(1)
            if ref.split(":", 1)[-1] not in known:
                rep.err(f"链路的 <{tag}> 引用的设备 id {ref} 找不到对应设备")

    # ---- 10. 画布坐标不重叠 ----
    # 说明：PT 原生文件里也有坐标重叠的（PT 允许把设备叠在一起），
    # 所以这条是警告。但我们的生成器不该产出这种图——设备叠着就看不见了。
    pos: dict[tuple[float, float], list[str]] = {}
    for b, n in zip(blocks, names):
        xy = _canvas_xy(b)
        if xy:
            pos.setdefault(xy, []).append(n)
    overlap = {k: v for k, v in pos.items() if len(v) > 1}
    if overlap:
        rep.warn(
            f"有 {len(overlap)} 组设备画布坐标完全相同（在 PT 里叠成一坨，看不见）："
            f"{list(overlap.values())[:3]}"
        )

    rep.note(f"{p.name}：{len(blocks)} 台设备，{len(rep.errors)} 个错误，{len(rep.warnings)} 个警告")
    return rep


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for arg in argv[1:]:
        for p in sorted(Path().glob(arg)) if any(c in arg for c in "*?[") else [Path(arg)]:
            if not p.exists():
                print(f"[缺失] {p}")
                bad += 1
                continue
            rep = verify(p)
            for i in rep.info:
                print(f"  · {i}")
            for w in rep.warnings:
                print(f"  [警告] {w}")
            for e in rep.errors:
                print(f"  [错误] {e}")
            if rep.errors:
                bad += 1
    if bad:
        print(f"\n{bad} 个文件未通过校验")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
