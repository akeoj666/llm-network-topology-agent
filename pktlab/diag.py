"""配置级故障诊断与修复（C3 的「给一份坏配置让我修」入口）。

现有的 `faults.py` / `repair.py` 是在 **DSL** 上注错和修复——那是「设计图纸写错了」。
评分标准 C3 要的是另一件事：**给一份已经下发的 IOS 配置（running-config），
找出哪里写错了并改对**。本模块补的就是这一段。

三层：
    parse()   把 IOS 配置文本解析成模型（接口/地址/路由/VLAN/ACL），保留行号
    check()   对模型做语义检查，产出 Issue（可修 / 不可修分开）
    fix()     可修的按行打补丁；不可修的如实标出来，不瞎改

刻意不做的：
- 不解析通用 IOS，只认本项目会生成的那批命令（多一行不认识的就跳过）
- 不推断 PC 与网关——running-config 里没有主机，网关类检查要配一份拓扑才做
- 修完的配置照样要过 `cmdguard` 白名单，才承认「可直接运行」
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

_MAX_ROUNDS = 3

_MASK_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_NET_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?:\s+(\d{1,3}(?:\.\d{1,3}){3}))?$")


# ---------------------------------------------------------------- 模型
@dataclass
class Iface:
    name: str
    iface_line: int
    ip_line: int | None = None
    addr: str | None = None
    mask: str | None = None
    shutdown: bool = False
    dot1q: int | None = None  # 子接口的 encapsulation dot1Q n
    acl_in: list[tuple[str, int]] = field(default_factory=list)  # (ACL 号, 行号)
    access_vlan: int | None = None
    mode: str | None = None

    @property
    def has_addr(self) -> bool:
        return bool(self.addr and self.mask)

    def prefix(self) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Interface] | None:
        if not self.has_addr:
            return None
        try:
            iface = ipaddress.ip_interface(f"{self.addr}/{self.mask}")
        except ValueError:
            return None
        return iface.network, iface


@dataclass
class Device:
    name: str
    lines: list[str]
    ifaces: dict[str, Iface] = field(default_factory=dict)
    rip: dict | None = None
    ospf: dict | None = None
    vlans: dict[int, int] = field(default_factory=dict)  # vlan id -> 行号
    acls: dict[str, int] = field(default_factory=dict)  # acl 号 -> 行号
    statics: list[int] = field(default_factory=list)


@dataclass
class Issue:
    code: str
    device: str
    where: str
    message: str
    fixable: bool = True
    hint: str = ""

    def __str__(self) -> str:
        flag = "可修" if self.fixable else "需人工"
        return f"[{flag}/{self.code}] {self.device} {self.where} — {self.message}"


@dataclass
class Result:
    rounds: int
    issues_before: list[Issue]
    issues_after: list[Issue]
    fixes: list[str]
    configs: dict[str, list[str]]

    @property
    def ok(self) -> bool:
        return not [i for i in self.issues_after if i.fixable]


# ---------------------------------------------------------------- 解析
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse(configs: dict[str, list[str]]) -> dict[str, Device]:
    """把 {设备名: 配置行} 解析成模型。不认识的行原样留在 lines 里。"""
    out: dict[str, Device] = {}
    for name, lines in configs.items():
        dev = Device(name=name, lines=list(lines))
        cur: Iface | None = None
        block: str | None = None  # interface / rip / ospf
        for i, raw in enumerate(lines):
            s = raw.strip()
            if not s or s == "!":
                continue
            low = s.lower()

            if low.startswith("hostname "):
                dev.name = s.split(None, 1)[1]
                cur, block = None, None
                continue

            if low.startswith("interface "):
                iname = s.split(None, 1)[1]
                cur = Iface(name=iname, iface_line=i)
                dev.ifaces[iname] = cur
                block = "interface"
                continue

            if low.startswith("router rip"):
                dev.rip = {"version": None, "auto_summary": None,
                           "networks": [], "start": i, "end": None}
                cur, block = None, "rip"
                continue
            if low.startswith("router ospf "):
                proc = s.split()[-1]
                dev.ospf = {"process": proc, "networks": [], "start": i, "end": None}
                cur, block = None, "ospf"
                continue
            if low.startswith("vlan ") and low[5:].strip().isdigit():
                dev.vlans[int(low[5:].strip())] = i
                cur, block = None, "vlan"
                continue
            if low.startswith("access-list "):
                acl_id = s.split()[1]
                dev.acls.setdefault(acl_id, i)
                dev.statics, block = dev.statics, "acl"
                continue
            if low.startswith("ip route "):
                dev.statics.append(i)
                cur, block = None, None
                continue
            if " access-group " in " " + low:
                m = re.search(r"ip access-group\s+(\S+)\s+(\w+)", low)
                if m and cur is not None:
                    cur.acl_in.append((m.group(1), i))
                continue

            # 块内指令
            if block == "interface" and cur is not None and _indent(raw) >= 1:
                if low.startswith("ip address "):
                    parts = s.split()
                    if len(parts) >= 4:
                        cur.addr, cur.mask, cur.ip_line = parts[2], parts[3], i
                elif low == "no shutdown":
                    cur.shutdown = False
                elif low == "shutdown":
                    cur.shutdown = True
                elif low.startswith("encapsulation dot1q "):
                    try:
                        cur.dot1q = int(s.split()[-1])
                    except ValueError:
                        pass
                elif low.startswith("switchport access vlan "):
                    try:
                        cur.access_vlan = int(s.split()[-1])
                    except ValueError:
                        pass
                elif low.startswith("switchport mode "):
                    cur.mode = s.split()[-1].lower()
                continue

            if block == "rip" and dev.rip is not None and _indent(raw) >= 1:
                if low.startswith("version "):
                    try:
                        dev.rip["version"] = int(low.split()[-1])
                    except ValueError:
                        pass
                elif low.startswith("no auto-summary"):
                    dev.rip["auto_summary"] = False
                elif low.startswith("auto-summary"):
                    dev.rip["auto_summary"] = True
                elif low.startswith("network "):
                    dev.rip["networks"].append((s.split(None, 1)[1].strip(), i))
                continue

            if block == "ospf" and dev.ospf is not None and _indent(raw) >= 1:
                m = re.match(r"network\s+(\S+)\s+(\S+)\s+area\s+(\S+)", low)
                if m:
                    dev.ospf["networks"].append((m.group(1), m.group(2), m.group(3), i))
                continue

            # 顶层语句打断块
            if _indent(raw) == 0:
                cur, block = None, None
        out[dev.name] = dev
    return out


# ---------------------------------------------------------------- 检查
def _direct_nets(dev: Device) -> list[tuple[ipaddress.IPv4Network, Iface]]:
    out = []
    for itf in dev.ifaces.values():
        got = itf.prefix()
        if got:
            out.append((got[0], itf))
    return out


def check(devs: dict[str, Device]) -> list[Issue]:
    issues: list[Issue] = []

    # ① 地址与掩码本身
    for dev in devs.values():
        seen: dict[str, str] = {}
        for itf in dev.ifaces.values():
            if not itf.has_addr:
                continue
            try:
                mask = ipaddress.IPv4Network(f"0.0.0.0/{itf.mask}").netmask
            except ValueError:
                issues.append(Issue("BAD_MASK", dev.name, f"{itf.name}.ip address",
                                    f"掩码 {itf.mask} 不是合法点分掩码", fixable=False))
                continue
            if str(mask) != itf.mask:
                issues.append(Issue("BAD_MASK", dev.name, f"{itf.name}.ip address",
                                    f"掩码 {itf.mask} 不连续，应为 {mask}", fixable=False))
                continue
            got = itf.prefix()
            if got is None:
                issues.append(Issue("BAD_ADDR", dev.name, f"{itf.name}.ip address",
                                    f"{itf.addr} 与 {itf.mask} 拼不出合法接口地址", fixable=False))
                continue
            net, iface = got
            if iface.ip == net.network_address and net.prefixlen < 31:
                issues.append(Issue("BAD_ADDR", dev.name, f"{itf.name}.ip address",
                                    f"{itf.addr} 是网段号 {net}，不能配在接口上", fixable=False))
            elif net.prefixlen < 31 and iface.ip == net.broadcast_address:
                issues.append(Issue("BAD_ADDR", dev.name, f"{itf.name}.ip address",
                                    f"{itf.addr} 是广播地址 {net}，不能配在接口上", fixable=False))
            if itf.addr in seen:
                issues.append(Issue("DUP_IP", dev.name, f"{itf.name}.ip address",
                                    f"{itf.addr} 与 {seen[itf.addr]} 重复", fixable=False))
            seen[itf.addr] = itf.name

    # ② 同一链路上掩码不一致（两端互相落在对方掩码内，却算出不同网段）
    items = [(d.name, itf.name, itf) for d in devs.values() for itf in d.ifaces.values() if itf.has_addr]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            n1, i1, a = items[i]
            n2, i2, b = items[j]
            if n1 == n2:
                continue
            try:
                ia = ipaddress.ip_interface(f"{a.addr}/{a.mask}")
                ib = ipaddress.ip_interface(f"{b.addr}/{b.mask}")
            except ValueError:
                continue
            if ia.ip in ib.network and ib.ip in ia.network and ia.network != ib.network:
                # 取能同时装下两个地址的最长前缀，就是双方该共用的掩码
                want = ipaddress.ip_network(f"{ia.ip}/{32}", strict=False)
                for p in range(31, 7, -1):
                    cand = ipaddress.ip_network(f"{ia.ip}/{p}", strict=False)
                    if ib.ip in cand:
                        want = cand
                        break
                issues.append(Issue(
                    "MASK_MISMATCH", n1, f"{n1} {i1} ↔ {n2} {i2}",
                    f"{n1} 的 {a.addr}/{a.mask}（{ia.network}）与 {n2} 的 {b.addr}/{b.mask}"
                    f"（{ib.network}）互不承认，应统一为 {want.netmask}（{want}）",
                    fixable=True, hint=str(want.netmask)))

    # ③ 路由协议的 network 覆盖不到直连网段
    for dev in devs.values():
        nets = _direct_nets(dev)
        if dev.rip is not None:
            declared = []
            for text, _ in dev.rip["networks"]:
                m = _NET_RE.match(text)
                if m:
                    declared.append(m.group(1))
            for net, itf in nets:
                major = _classful(itf)
                if major is None:
                    continue
                if major not in declared:
                    issues.append(Issue(
                        "RIP_MISSING_NET", dev.name, f"router rip / {itf.name}",
                        f"直连网段 {net} 属于主网络 {major}，但 router rip 里没有 "
                        f"`network {major}`", fixable=True, hint=major))
        if dev.ospf is not None:
            area = dev.ospf["networks"][0][2] if dev.ospf["networks"] else "0"
            for net, itf in nets:
                if not _ospf_covered(net, dev.ospf["networks"]):
                    issues.append(Issue(
                        "OSPF_MISSING_NET", dev.name, f"router ospf / {itf.name}",
                        f"直连网段 {net} 没有被任何 network/wildcard 覆盖，"
                        f"邻居学不到它", fixable=True,
                        hint=f"{net.network_address} {net.hostmask} area {area}"))

    # ④ VLAN 引用（只看交换机的 switchport；路由器子接口的 dot1Q 不需要 `vlan` 语句）
    for dev in devs.values():
        for itf in dev.ifaces.values():
            vid = itf.access_vlan
            if vid is None:
                continue
            if vid not in dev.vlans:
                issues.append(Issue(
                    "VLAN_UNDEFINED", dev.name, f"{itf.name} → vlan {vid}",
                    f"接口把端口划进了 VLAN {vid}，但本设备没有 `vlan {vid}`，"
                    f"PT 里这个 VLAN 不会出现、端口会挂在 VLAN 1 上", fixable=True, hint=str(vid)))

    # ⑤ ACL 引用
    for dev in devs.values():
        for itf in dev.ifaces.values():
            for acl, _ in itf.acl_in:
                if acl not in dev.acls:
                    issues.append(Issue(
                        "ACL_UNKNOWN", dev.name, f"{itf.name} → access-group {acl}",
                        f"接口挂了 ACL {acl}，但本设备没有 `access-list {acl}` 任何条目"
                        f"（效果等于全放行，与意图相反）", fixable=False))

    return issues


def _classful(itf: Iface) -> str | None:
    """接口地址所属的有类主网络（RIP 的 network 就写这个）。"""
    try:
        ip = ipaddress.ip_address(itf.addr or "")
    except ValueError:
        return None
    first = int(str(ip).split(".")[0])
    if first < 128:
        return f"{first}.0.0.0"
    if first < 192:
        return ".".join(str(ip).split(".")[:2]) + ".0.0"
    return ".".join(str(ip).split(".")[:3]) + ".0"


def _ospf_covered(net: ipaddress.IPv4Network, declared: list[tuple]) -> bool:
    for addr, wildcard, _, _ in declared:
        try:
            wc = ipaddress.IPv4Address(wildcard)
            base = ipaddress.IPv4Address(addr)
        except ValueError:
            continue
        # wildcard 取反就是掩码
        mask = ipaddress.IPv4Address(int(wc) ^ 0xFFFFFFFF)
        try:
            cover = ipaddress.ip_network(f"{base}/{mask}", strict=False)
        except ValueError:
            continue
        if net.subnet_of(cover):
            return True
    return False


# ---------------------------------------------------------------- 修复
def fix(devs: dict[str, Device], issues: list[Issue]) -> list[str]:
    """按 Issue 打补丁。只动可修的，且每处改动都记下来。"""
    done: list[str] = []

    # 掩码不一致：两端都改成 want
    for iss in [i for i in issues if i.code == "MASK_MISMATCH" and i.fixable]:
        left, right = iss.where.split(" ↔ ")
        n1, i1 = left.split(" ", 1)
        parts = right.split(maxsplit=1)
        n2, i2 = parts[0], parts[1]
        for dev_name, if_name in ((n1, i1), (n2, i2)):
            dev = devs[dev_name]
            itf = dev.ifaces.get(if_name)
            if not itf or itf.ip_line is None:
                continue
            if itf.mask == iss.hint:
                continue
            old = dev.lines[itf.ip_line]
            indent = " " * (_indent(old) or 1)
            dev.lines[itf.ip_line] = f"{indent}ip address {itf.addr} {iss.hint}"
            itf.mask = iss.hint
            done.append(f"{dev_name} {if_name}：掩码 {old.strip().split()[-1]} → {iss.hint}")
        iss.fixable = False  # 已处理，别重复修

    # 漏 network：追加到对应路由块末尾
    for iss in [i for i in issues if i.code == "RIP_MISSING_NET"]:
        dev = devs[iss.device]
        line = f" network {iss.hint}"
        if dev.rip is None or line in dev.lines:
            continue
        # 插在 router rip 块的最后一条 network 之后
        idx = max([i for _, i in dev.rip["networks"]], default=dev.rip["start"])
        dev.lines.insert(idx + 1, line)
        done.append(f"{dev.name}：补 `{line.strip()}`（直连主网络漏宣告）")

    for iss in [i for i in issues if i.code == "OSPF_MISSING_NET"]:
        dev = devs[iss.device]
        line = f" network {iss.hint}"
        if dev.ospf is None or line in dev.lines:
            continue
        idx = max([i for *_, i in dev.ospf["networks"]], default=dev.ospf["start"])
        dev.lines.insert(idx + 1, line)
        done.append(f"{dev.name}：补 `{line.strip()}`（直连网段没被 network 覆盖）")

    # VLAN 未定义
    for iss in [i for i in issues if i.code == "VLAN_UNDEFINED"]:
        dev = devs[iss.device]
        vid = int(iss.hint)
        if vid in dev.vlans:
            continue
        if any(ln.strip().lower() == f"vlan {vid}" for ln in dev.lines):
            continue
        # 插在第一个 interface 之前（IOS 把 vlan 数据库放在配置顶部），
        # 只补 `vlan N`，**不编名字**——名字不在坏配置里，猜一个不如留空
        at = next((i for i, ln in enumerate(dev.lines)
                   if ln.strip().lower().startswith("interface ")), len(dev.lines))
        dev.lines[at:at] = [f"vlan {vid}"]
        dev.vlans[vid] = at
        done.append(f"{dev.name}：补 `vlan {vid}`（接口引用了但没建；名字不在配置里，未编造）")

    return done


# ---------------------------------------------------------------- 闭环
def diagnose_and_fix(
    configs: dict[str, list[str]],
    *,
    max_rounds: int = _MAX_ROUNDS,
) -> Result:
    """读坏配置 → 定位 → 修 → 复检，最多 max_rounds 轮。"""
    devs = parse(configs)
    before = check(devs)
    fixes: list[str] = []
    rounds = 0

    while rounds < max_rounds:
        todo = [i for i in before if i.fixable]
        if not todo:
            break
        rounds += 1
        fixes += fix(devs, before)
        devs = parse({d.name: d.lines for d in devs.values()})
        before = check(devs)

    after = before
    return Result(
        rounds=rounds,
        issues_before=before if rounds == 0 else _initial(devs, configs),
        issues_after=after,
        fixes=fixes,
        configs={d.name: d.lines for d in devs.values()},
    )


def _initial(devs: dict[str, Device], configs: dict[str, list[str]]) -> list[Issue]:
    """修复前的问题清单（重新解析原始输入算一遍，避免被就地修改影响）。"""
    return check(parse(configs))
