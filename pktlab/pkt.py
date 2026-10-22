"""Packet Tracer .pkt 文件的编辑层。

设计原则：**只改必须改的字符，其余字节原样保留**。
做法是把 XML 按 <DEVICE> 边界切成「骨架文本 + 设备块数组 + 尾部文本」，
所有编辑都是对单个设备块做定点字符串替换，不做 XML 重新序列化
（ElementTree 回写会改变缩进、属性引号、自闭合标签，徒增风险）。

已验证：对原始文件不做任何编辑时，重编码输出与原文件字节完全一致。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .codec import encrypt_pka, decrypt_pka, read_pt_file, write_pt_file

DEVICE_RE = re.compile(r"<DEVICE>.*?</DEVICE>", re.S)

_NAME_RE = re.compile(
    r"(<ENGINE>\s*<TYPE[^>]*>[^<]*</TYPE>\s*<NAME[^>]*>)([^<]*)(</NAME>)", re.S
)
_MODEL_RE = re.compile(r'<ENGINE>\s*<TYPE[^>]*model="([^"]*)"[^>]*>([^<]*)</TYPE>', re.S)
_REF_RE = re.compile(r"<SAVE_REF_ID>([^<]*)</SAVE_REF_ID>")
_CONF_RE = re.compile(r"<RUNNINGCONFIG>(.*?)</RUNNINGCONFIG>", re.S)
_START_RE = re.compile(r"<STARTUPCONFIG>(.*?)</STARTUPCONFIG>", re.S)
_XY_RE = re.compile(r"(<LOGICAL>\s*<X>)([^<]*)(</X>\s*<Y>)([^<]*)(</Y>)", re.S)
_IP_RE = re.compile(r"<IP>([^<]*)</IP>")
_MASK_RE = re.compile(r"<SUBNET>([^<]*)</SUBNET>")
_GW_RE = re.compile(r"<PORT_GATEWAY>([^<]*)</PORT_GATEWAY>")
_SYS_GW_RE = re.compile(r"<GATEWAY>([^<]*)</GATEWAY>")
_VLANS_RE = re.compile(r"(<VLANS>)(.*?)(</VLANS>)", re.S)
_VLAN_NUM_RE = re.compile(r'number="(\d+)"')
_VLAN_COUNT_RE = re.compile(r"<VLAN_COUNT>(\d+)</VLAN_COUNT>")

# 交换机自带的系统 VLAN，必须保留
_SYSTEM_VLANS = (1002, 1003, 1004, 1005)
_SYSTEM_NAME = {
    1002: "fddi-default",
    1003: "token-ring-default",
    1004: "fddinet-default",
    1005: "trnet-default",
}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unesc(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class Device:
    """单台设备的视图，改动立即写回到所属 Pkt 的块数组。"""

    def __init__(self, pkt: "Pkt", index: int):
        self.pkt = pkt
        self.index = index

    # ---- 只读属性 ----
    @property
    def block(self) -> str:
        return self.pkt.blocks[self.index]

    @property
    def name(self) -> str:
        m = _NAME_RE.search(self.block)
        return _unesc(m.group(2)) if m else ""

    @property
    def model(self) -> str:
        m = _MODEL_RE.search(self.block)
        return m.group(1) if m else ""

    @property
    def kind(self) -> str:
        m = _MODEL_RE.search(self.block)
        return m.group(2) if m else ""

    @property
    def ref(self) -> str:
        m = _REF_RE.search(self.block)
        return m.group(1) if m else ""

    @property
    def config_lines(self) -> list[str]:
        m = _CONF_RE.search(self.block)
        if not m:
            return []
        return [_unesc(x) for x in re.findall(r"<LINE>(.*?)</LINE>", m.group(1), re.S)]

    @property
    def position(self) -> tuple[float, float]:
        m = _XY_RE.search(self.block)
        if not m:
            return (0.0, 0.0)
        return (float(m.group(2) or 0), float(m.group(4) or 0))

    @property
    def ip(self) -> str:
        m = _IP_RE.search(self.block)
        return m.group(1) if m else ""

    @property
    def has_cli(self) -> bool:
        return bool(self.config_lines)

    def __repr__(self) -> str:
        x, y = self.position
        return f"<{self.name} {self.model} @({x},{y}) cfg={len(self.config_lines)}>"

    # ---- 写入 ----
    def rename(self, new_name: str) -> "Device":
        m = _NAME_RE.search(self.block)
        if not m:
            raise ValueError(f"{self.name}: 找不到 NAME 节点")
        block = self.block[: m.start(2)] + _esc(new_name) + self.block[m.end(2) :]
        self.pkt.blocks[self.index] = block
        return self

    def set_config(self, lines: list[str]) -> "Device":
        """整段替换 running-config / startup-config。lines 为原始 IOS 配置行。"""
        block = self.block
        for pat in (_CONF_RE, _START_RE):
            m = pat.search(block)
            if not m:
                continue
            indent = "      "
            inner = m.group(1)
            im = re.search(r"\n([ \t]*)<LINE>", inner)
            if im:
                indent = im.group(1)
            body = "".join(f"\n{indent}<LINE>{_esc(ln)}</LINE>" for ln in lines)
            block = block[: m.start(1)] + body + "\n     " + block[m.end(1) :]
        self.pkt.blocks[self.index] = block
        return self

    def move(self, x: float, y: float) -> "Device":
        m = _XY_RE.search(self.block)
        if not m:
            raise ValueError(f"{self.name}: 找不到 WORKSPACE/LOGICAL 坐标")
        block = self.block[: m.start(2)] + f"{x:g}" + self.block[m.end(2) :]
        m2 = _XY_RE.search(block)
        block = block[: m2.start(4)] + f"{y:g}" + block[m2.end(4) :]
        self.pkt.blocks[self.index] = block
        return self

    def clear_port_ips(self) -> "Device":
        """清空所有端口上的 IP/掩码，只留 running-config 里的那份。

        为什么必须清：设备是从骨架 pkt 里克隆出来的，**端口节点里带着骨架自带的旧地址**
        （例如某台路由器的 Serial0/0 残留 `10.0.0.1`）。PT 载入时会先把端口地址装上去，
        再回放 running-config；如果配置里另一个口也写了同一个地址，**后写的那个会被判
        IP 冲突而拒绝**，现象是「`show run` 里有这一行、但接口既没有地址也不进 OSPF」。

        实测就踩到这一条：R0 的 Fa3/0 配 10.0.0.1/30，骨架的 Serial0/0 残留 10.0.0.1，
        结果 Fa3/0 拿不到地址 → R0 与 R2 建不起 OSPF 邻居 → 流量全走串口，
        `tracert` 只剩 3 跳、路由代价变 65。
        """
        block = self.block
        # 只清 <PORT> 里的 <IP>/<SUBNET>，别碰 PC 的 <GATEWAY> 等其它字段
        def _blank(m: re.Match) -> str:
            return _IP_RE.sub("<IP></IP>", _MASK_RE.sub("<SUBNET></SUBNET>", m.group(0)), count=1)

        block = re.sub(r"<PORT>.*?</PORT>", _blank, block, flags=re.S)
        self.pkt.blocks[self.index] = block
        return self

    def set_host_ip(self, ip: str, mask: str, gateway: str = "") -> "Device":
        """PC/Server 的网卡参数。

        注意：网关在两处各存一份 —— 端口里的 <PORT_GATEWAY> 和设备级的 <GATEWAY>。
        Packet Tracer 的 IP Configuration 界面读的是**设备级**那个，只改端口那份
        会出现「IP 变了、网关没变」的现象（实测确认）。
        """
        block = self.block
        block = _IP_RE.sub(f"<IP>{_esc(ip)}</IP>", block, count=1)
        block = _MASK_RE.sub(f"<SUBNET>{_esc(mask)}</SUBNET>", block, count=1)
        if gateway:
            block = _GW_RE.sub(f"<PORT_GATEWAY>{_esc(gateway)}</PORT_GATEWAY>", block, count=1)
            block = _SYS_GW_RE.sub(f"<GATEWAY>{_esc(gateway)}</GATEWAY>", block)
        self.pkt.blocks[self.index] = block
        return self


class Pkt:
    def __init__(self, xml: str):
        self.parts = DEVICE_RE.split(xml)
        self.blocks: list[str] = DEVICE_RE.findall(xml)
        self.tail = self.parts[-1]

    # ---- 读写 ----
    @classmethod
    def open(cls, path: str | Path) -> "Pkt":
        return cls(read_pt_file(path).decode("utf-8"))

    @classmethod
    def from_bytes(cls, blob: bytes) -> "Pkt":
        return cls(decrypt_pka(blob).decode("utf-8"))

    def to_xml(self) -> str:
        out = [self.parts[0]]
        for i, blk in enumerate(self.blocks):
            out.append(blk)
            out.append(self.parts[i + 1])
        return "".join(out)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encrypt_pka(self.to_xml().encode("utf-8")))
        return path

    # ---- 查询 ----
    def devices(self) -> list[Device]:
        return [Device(self, i) for i in range(len(self.blocks))]

    def by_name(self, name: str) -> Device:
        for d in self.devices():
            if d.name == name:
                return d
        raise KeyError(f"没有名为 {name} 的设备")

    def inventory(self) -> list[tuple[str, str, str, int]]:
        return [(d.name, d.model, d.ref, len(d.config_lines)) for d in self.devices()]

    def links(self) -> list[str]:
        """NETWORK/LINKS 里的每条链路（原始 XML 片段）。"""
        xml = self.to_xml()
        m = re.search(r"<LINKS>(.*?)</LINKS>", xml, re.S)
        if not m:
            return []
        return re.findall(r"<LINK>.*?</LINK>", m.group(1), re.S)

    def dev_addr(self, name: str) -> str:
        """设备的 DEV_ADDR —— 链路的 *_DEVICE_MEM_ADDR 就是引用它（已实测验证）。"""
        blk = self.by_name(name).block
        m = re.search(r"<WORKSPACE>\s*<LOGICAL>.*?<MEM_ADDR>[^<]*</MEM_ADDR>\s*<DEV_ADDR>([^<]*)</DEV_ADDR>", blk, re.S)
        if not m:
            raise ValueError(f"{name}: 找不到 DEV_ADDR")
        return m.group(1)

    def _next_port_addr(self) -> int:
        """端口内存地址是 PT 运行时分配的，文件里不留存；这里造一个不冲突的。"""
        used = [int(x) for x in re.findall(r"<(?:FROM|TO)_PORT_MEM_ADDR>(\d+)<", self.to_xml())]
        return (max(used) + 8192) if used else 139850990000000

    def clear_links(self) -> "Pkt":
        """删掉全部链路（重建拓扑时用）。"""
        xml = re.sub(r"<LINKS>.*?</LINKS>", "<LINKS></LINKS>", self.to_xml(), flags=re.S)
        self.__init__(xml)
        return self

    def add_link(
        self,
        a: tuple[str, str],
        b: tuple[str, str],
        cable: str = "serial",
        dce_side: str = "b",
        template: str | None = None,
    ) -> "Pkt":
        """新建一条链路。a/b = (设备名, 端口名)。

        做法：克隆一条同类型的既有链路（保留 LENGTH/颜色等字段），
        替换两端引用、设备内存地址，并重新分配端口内存地址。
        """
        outer = "eSerial" if cable == "serial" else "eCopper"
        tpl = template
        if tpl is None:
            templates = [ln for ln in self.links() if f"<TYPE>{outer}</TYPE>" in ln]
            if not templates:
                raise ValueError(f"骨架里没有 {outer} 类型的链路可供克隆")
            tpl = templates[-1]

        a_name, a_port = a
        b_name, b_port = b
        base = self._next_port_addr()
        new = tpl
        # 先动 TO 侧（避免 FROM 替换后位置偏移影响）
        new = re.sub(r"(<TO>)[^<]*(</TO>)", lambda m: m.group(1) + self.by_name(b_name).ref + m.group(2), new)
        new = re.sub(r"(<TO_DEVICE_MEM_ADDR>)[^<]*(</TO_DEVICE_MEM_ADDR>)",
                     lambda m: m.group(1) + self.dev_addr(b_name) + m.group(2), new)
        new = re.sub(r"(<TO_PORT_MEM_ADDR>)[^<]*(</TO_PORT_MEM_ADDR>)",
                     lambda m: m.group(1) + str(base + 8192) + m.group(2), new)
        new = re.sub(r"(<FROM>)[^<]*(</FROM>)", lambda m: m.group(1) + self.by_name(a_name).ref + m.group(2), new)
        new = re.sub(r"(<FROM_DEVICE_MEM_ADDR>)[^<]*(</FROM_DEVICE_MEM_ADDR>)",
                     lambda m: m.group(1) + self.dev_addr(a_name) + m.group(2), new)
        new = re.sub(r"(<FROM_PORT_MEM_ADDR>)[^<]*(</FROM_PORT_MEM_ADDR>)",
                     lambda m: m.group(1) + str(base) + m.group(2), new)

        # 端口名：CABLE 里两个 <PORT> 按出现顺序分别是 FROM 端、TO 端，一次遍历改掉
        state = {"i": 0}
        wanted = [a_port, b_port]

        def _port_sub(m: re.Match) -> str:
            i = state["i"]
            state["i"] += 1
            return f"<PORT>{wanted[i]}</PORT>" if i < len(wanted) else m.group(0)

        new = re.sub(r"<PORT>[^<]*</PORT>", _port_sub, new)

        # 交叉线：只是 CABLE 里的线型标记不同（外层 TYPE 仍是 eCopper）
        if cable == "cross":
            new = new.replace("<TYPE>eStraightThrough</TYPE>", "<TYPE>eCrossOver</TYPE>")

        if cable == "serial":
            dce_name, dce_port = (b_name, b_port) if dce_side == "b" else (a_name, a_port)
            new = re.sub(r"(<DCEDEV>)[^<]*(</DCEDEV>)",
                         lambda m: m.group(1) + self.by_name(dce_name).ref + m.group(2), new)
            new = re.sub(r"(<DCEPORT>)[^<]*(</DCEPORT>)",
                         lambda m: m.group(1) + dce_port + m.group(2), new)

        xml = self.to_xml()
        idx = xml.find("</LINKS>")
        xml = xml[:idx] + "   " + new + "\n  " + xml[idx:]
        self.__init__(xml)
        return self

    def remove_device(self, name: str) -> "Pkt":
        """删除设备，并连带删除所有引用它的链路。"""
        target = self.by_name(name)
        ref = target.ref
        keep = [b for i, b in enumerate(self.blocks) if i != target.index]
        xml = self.parts[0]
        for i, blk in enumerate(keep):
            xml += blk + self.parts[i + 1]
        xml += self.parts[-1]
        # 删掉引用了该设备 save-ref-id 的链路
        xml = re.sub(
            r"<LINK>.*?</LINK>", lambda m: "" if ref in m.group(0) else m.group(0), xml, flags=re.S
        )
        self.__init__(xml)
        return self

    # ---- 画布便签 ----
    def set_notes(self, items: list[tuple[str, float, float]]) -> "Pkt":
        """整体重写画布上的文字便签（那些 IP / 网段注释）。

        便签是独立于设备的对象，带自己的 X/Y，不会跟着设备移动。
        所以每次生成都要按新坐标重写一遍，否则会出现「线错开了、注释没跟上」。
        """
        xml = self.to_xml()
        blocks = list(re.finditer(r"<NOTES>(.*?)</NOTES>", xml, re.S))
        if not blocks:
            return self
        target = next((m for m in blocks if "<NOTE uuid" in m.group(1)), blocks[-1])
        indent = "   "
        body = ""
        for i, (text, x, y) in enumerate(items):
            body += (
                f'\n{indent}<NOTE uuid="{{{uuid.uuid4()}}}">'
                f'\n{indent} <X>{x:.1f}</X>'
                f'\n{indent} <Y>{y:.1f}</Y>'
                f'\n{indent} <Z>{40000 + i}</Z>'
                f'\n{indent} <TEXT translate="true">{_esc(text)}</TEXT>'
                f'\n{indent} <NOTECLUSTERID>1-1</NOTECLUSTERID>'
                f'\n{indent} <MEM_ADDR>{900000000 + i}</MEM_ADDR>'
                f'\n{indent}</NOTE>'
            )
        xml = xml[: target.start(1)] + body + "\n  " + xml[target.end(1) :]
        self.__init__(xml)
        return self

    # ---- 全局处理 ----
    def set_vlans(self, name: str, vlans: list[tuple[int, str]]) -> "Pkt":
        """重写交换机的 VLAN 数据库（等价于 vlan.dat）。

        实测教训：只在 RUNNINGCONFIG 里写 `vlan 10` / `name RD` 是不够的。
        PT 的 `show vlan brief` 读的是 XML 里的 <VLANS> 列表，运行配置里那两行
        既不报错也不生效——结果就是端口被 `switchport access vlan 10` 划走了
        （从 VLAN1 的端口列表里消失），但 VLAN10 本身不存在，
        于是同 VLAN 内都互相 ping 不通。

        一台设备里可能有两份：运行态的 <VLANS>，以及 flash 里 vlan.dat 的
        副本（<FILE_CONTENT class="CVlanDatFileContent">），两份都要改。
        """
        dev = self.by_name(name)
        block = dev.block
        if not _VLANS_RE.search(block):
            return self

        wanted = sorted(vlans)

        def _rewrite(m: re.Match) -> str:
            inner = m.group(2)
            entries = re.findall(r"<VLAN\b[^>]*/>", inner)
            if not entries:
                return m.group(0)
            num = lambda e: int(_VLAN_NUM_RE.search(e).group(1))  # noqa: E731
            base = next((e for e in entries if num(e) == 1), entries[0])
            im = re.search(r"\n([ \t]*)<VLAN\b", inner)
            indent = im.group(1) if im else "      "
            tail = inner[inner.rfind("\n"):] if "\n" in inner else "\n     "

            ordered: list[str] = []
            # 1) 保留 default
            for e in entries:
                if num(e) == 1:
                    ordered.append(e)
                    break
            # 2) 新 VLAN：沿用 default 那条的属性顺序，只换 number / name
            for vid, vname in wanted:
                e = base.replace('number="1"', f'number="{vid}"', 1)
                e = e.replace('name="default"', f'name="{_esc(vname)}"', 1)
                ordered.append(e)
            # 3) 保留系统 VLAN
            for e in entries:
                if num(e) in _SYSTEM_VLANS:
                    ordered.append(e)

            body = "".join(f"\n{indent}{e}" for e in ordered) + tail
            return m.group(1) + body + m.group(3)

        block = _VLANS_RE.sub(_rewrite, block)
        block = _VLAN_COUNT_RE.sub(
            f"<VLAN_COUNT>{len(_SYSTEM_VLANS) + 1 + len(wanted)}</VLAN_COUNT>", block
        )

        # 种子里的交换机若从没配过 VLAN，flash 里就没有 vlan.dat 这个文件。
        # PT 真实设备是「启动时读 flash:vlan.dat」，补一份让两种读法都能对上。
        if "CVlanDatFileContent" not in block:
            block = self._inject_vlan_dat(block, wanted)

        self.blocks[dev.index] = block
        return self

    @staticmethod
    def _inject_vlan_dat(block: str, wanted: list[tuple[int, str]]) -> str:
        flash = re.search(r"\n(\s*)</FILES>\s*\n\s*<FILE_COUNTER>(\d+)</FILE_COUNTER>", block)
        if not flash:
            return block
        ind = flash.group(1)
        counter = int(flash.group(2))
        nums = [int(x) for x in re.findall(r"<FILE_NUMBER>(\d+)</FILE_NUMBER>", block)]
        file_no = (max(nums) + 1) if nums else 2
        count = len(_SYSTEM_VLANS) + 1 + len(wanted)

        vtps = re.search(r"<VTP>.*?</VTP>", block, re.S)
        vtp = vtps.group(0) if vtps else (
            "<VTP><DOMAIN_NAME></DOMAIN_NAME><MODE>0</MODE><VERSION>1</VERSION>"
            "<PASSWORD></PASSWORD><CONFIG_REVISION>1</CONFIG_REVISION>"
            "<UPDATER_IP>0.0.0.0</UPDATER_IP><UPDATE_TIMESTAMP>0-0-00 00:00:00</UPDATE_TIMESTAMP></VTP>"
        )

        p = ind + " "
        out = f'\n{p}<FILE class="CFile">'
        out += f"\n{p} <FILE_NUMBER>{file_no}</FILE_NUMBER>"
        out += f"\n{p} <NAME>vlan.dat</NAME>"
        out += f"\n{p} <DATE_TIME>0</DATE_TIME>"
        out += f"\n{p} <PERMISSION>6</PERMISSION>"
        out += f'\n{p} <FILE_CONTENT class="CVlanDatFileContent">'
        out += f"\n{p}  <VLAN_COUNT>{count}</VLAN_COUNT>"
        out += f"\n{p}  <VLANS>"
        out += f'\n{p}   <VLAN rspan="0" number="1" name="default"/>'
        for vid, vname in wanted:
            out += f'\n{p}   <VLAN rspan="0" number="{vid}" name="{_esc(vname)}"/>'
        for vid in _SYSTEM_VLANS:
            out += f'\n{p}   <VLAN rspan="0" number="{vid}" name="{_SYSTEM_NAME[vid]}"/>'
        out += f"\n{p}  </VLANS>"
        out += f"\n{p}  {vtp}"
        out += f"\n{p} </FILE_CONTENT>"
        out += f"\n{p}</FILE>"

        block = block[: flash.start()] + out + block[flash.start():]
        block = re.sub(r"<FILE_COUNTER>\d+</FILE_COUNTER>",
                       f"<FILE_COUNTER>{max(counter, file_no + 1)}</FILE_COUNTER>", block)
        return block

    def clear_command_logs(self) -> "Pkt":
        """清掉种子文件里原作者的 CLI 历史（十六进制编码的命令日志）。"""
        xml = self.to_xml()
        i = xml.find("<COMMAND_LOGS>")
        j = xml.find("</COMMAND_LOGS>")
        if i >= 0 and j > i:
            xml = xml[: i + len("<COMMAND_LOGS>")] + "\n " + xml[j:]
            self.__init__(xml)
        return self

    def set_version(self, version: str) -> "Pkt":
        xml = re.sub(r"<VERSION>[^<]*</VERSION>", f"<VERSION>{version}</VERSION>", self.to_xml(), count=1)
        self.__init__(xml)
        return self
