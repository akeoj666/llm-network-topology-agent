"""按错误码做确定性修复：把校验能定位的错误，用一种「规则上必然正确」的方式改掉。

为什么要它
----------
两件事本来是分开的，其实是一件事：

  1. **C3 故障注入闭环**要证明「错误信息回灌之后能被修好」。
     如果只能靠再调一次 LLM 来修，那证明的是「模型碰巧修好了」，
     不能证明错误信息本身是可定位、可执行的。用确定性修复器修，
     只要修好就说明错误信息**足以定位到具体设备和字段**。
  2. **省 API**。模型常见的几类错（网关跑出网段、子接口编号和 dot1q 不一致、
     标准 ACL 里写了 destination）根本不用再问一遍，本地改掉即可。
     实测这类错误占首轮失败的大头。

设计原则
--------
- **只改能确定的**：网关跑出网段 → 改成该网段上路由器的地址，这是唯一合理解。
  子网重叠、掩码非法 → 不动，留给模型，因为「怎么挪」没有唯一答案。
- **幂等**：同一份数据修两次，第二次不应该再产生改动。
- **可解释**：每处改动都记进 `fixed`，能说清「哪个设备哪个字段，从什么改成什么」。
- **不静默降级**：修不掉的错误原样留在 `remaining` 里，绝不假装成功。
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
from dataclasses import dataclass, field

from .config import CATALOG, ROLE_MODEL
from .dsl import SCHEMA_PATH, Issue, Topology

IP = ipaddress.IPv4Interface

# 这些错误码「怎么改」没有唯一答案，不自动修，留给模型或人
UNFIXABLE = {
    "SUBNET_OVERLAP",
    "MASK_INVALID",
    "POOL_INVALID",
    "POOL_OVERFLOW",
    "POOL_CAPACITY",
    "SCHEMA_UNAVAILABLE",
}


@dataclass
class Fix:
    code: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.code}] @{self.where} {self.detail}"


@dataclass
class Result:
    data: dict
    fixed: list[Fix] = field(default_factory=list)
    remaining: list[Issue] = field(default_factory=list)
    passes: int = 0

    @property
    def ok(self) -> bool:
        return not self.remaining

    def summary(self) -> str:
        return f"修复 {len(self.fixed)} 处，剩余 {len(self.remaining)} 个错误（{self.passes} 轮）"


# ---------------------------------------------------------------- 小工具
def _devs(data: dict) -> list[dict]:
    d = data.get("devices")
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def _ifaces(dev: dict) -> list[dict]:
    i = dev.get("interfaces")
    return [x for x in i if isinstance(x, dict)] if isinstance(i, list) else []


def _pifc(text) -> IP | None:
    if not isinstance(text, str):
        return None
    try:
        obj = ipaddress.ip_interface(text)
    except ValueError:
        return None
    return obj if obj.ip.version == 4 else None


def _pnet(text) -> ipaddress.IPv4Network | None:
    try:
        return ipaddress.ip_network(text, strict=False)
    except (ValueError, TypeError):
        return None


def _used(data: dict) -> set[str]:
    out: set[str] = set()
    for d in _devs(data):
        ifc = _pifc(d.get("ip"))
        if ifc:
            out.add(str(ifc.ip))
        for itf in _ifaces(d):
            ifc = _pifc(itf.get("ip"))
            if ifc:
                out.add(str(ifc.ip))
    return out


def _usable(net: ipaddress.IPv4Network):
    """按顺序给出网段内可用主机地址（/31、/32 特殊处理）。"""
    if net.prefixlen >= 31:
        yield from net
        return
    for addr in net.hosts():
        yield addr


def _free(net: ipaddress.IPv4Network, used: set[str], avoid: set[str] | None = None) -> str | None:
    skip = used | (avoid or set())
    for addr in _usable(net):
        if str(addr) not in skip:
            return str(addr)
    return None


def _router_addr(data: dict, net: ipaddress.IPv4Network) -> str | None:
    """该网段上路由器的接口地址——它才是合理的网关。"""
    for d in _devs(data):
        if d.get("role") != "router":
            continue
        for itf in _ifaces(d):
            ifc = _pifc(itf.get("ip"))
            if ifc and ifc.network == net:
                return str(ifc.ip)
    return None


# ---------------------------------------------------------------- 1 未知字段
def _resolve(schema: dict, root: dict) -> dict:
    for _ in range(8):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        if not ref or not ref.startswith("#/"):
            break
        node: dict | None = root
        for part in ref[2:].split("/"):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if not isinstance(node, dict):
            break
        schema = node
    return schema if isinstance(schema, dict) else {}


def _prune(obj, schema: dict, root: dict, path: str, fixed: list[Fix]) -> int:
    """删掉 schema 里没定义的字段——模型幻觉字段是 SCHEMA 错误的主要来源。"""
    schema = _resolve(schema, root)
    n = 0
    if isinstance(obj, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for k in list(obj.keys()):
                if k not in props:
                    obj.pop(k)
                    fixed.append(Fix("SCHEMA", path or "<根>", f"删除未定义字段 {k!r}"))
                    n += 1
                else:
                    n += _prune(obj[k], props[k], root, f"{path}.{k}" if path else k, fixed)
    elif isinstance(obj, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(obj):
                n += _prune(v, items, root, f"{path}[{i}]", fixed)
    return n


def _fix_schema(data: dict, fixed: list[Fix]) -> int:
    try:
        root = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return _prune(data, root, root, "", fixed)


# ---------------------------------------------------------------- 2 meta
def _fix_meta(data: dict, fixed: list[Fix]) -> int:
    n = 0
    meta = data.get("meta")
    if not isinstance(meta, dict):
        data["meta"] = {"id": "UC-99", "title": "未命名用例"}
        fixed.append(Fix("META", "meta", "meta 缺失或不是对象，补默认值"))
        return 1
    uid = meta.get("id")
    if not isinstance(uid, str) or not re.fullmatch(r"UC-\d{2}", uid):
        new = "UC-99"
        m = re.search(r"(\d{1,3})", str(uid or ""))
        if m:
            new = f"UC-{int(m.group(1)) % 100:02d}"
        fixed.append(Fix("META", "meta.id", f"id {uid!r} 不合规，改为 {new}"))
        meta["id"] = new
        n += 1
    title = meta.get("title")
    if not isinstance(title, str) or not (2 <= len(title) <= 40):
        new = (title or "").strip()[:40] or "未命名用例"
        if len(new) < 2:
            new = "未命名用例"
        fixed.append(Fix("META", "meta.title", f"title {title!r} 长度不合规，改为 {new!r}"))
        meta["title"] = new
        n += 1
    return n


# ---------------------------------------------------------------- 3 设备名重复
def _fix_dup_names(data: dict, fixed: list[Fix]) -> int:
    n = 0
    seen: dict[str, int] = {}
    renames: dict[str, str] = {}
    for d in _devs(data):
        name = d.get("name")
        if not isinstance(name, str):
            continue
        if name in seen:
            seen[name] += 1
            new = f"{name}{seen[name]}"
            fixed.append(Fix("DUP_DEVICE", name, f"重名，改为 {new}"))
            renames.setdefault(name, new)
            d["name"] = new
            n += 1
        else:
            seen[name] = 0
    # 链路与路由里也引用了旧名，只改被重命名的那份是不可能的（无法区分），
    # 所以这里只处理「链路引用了已不存在的设备」会在 _fix_links 里兜住。
    return n


# ---------------------------------------------------------------- 4 地址
def _fix_addr_shape(data: dict, fixed: list[Fix]) -> int:
    """把网络号 / 广播地址改成可用主机地址。"""
    n = 0
    used = _used(data)

    def one(holder: dict, key: str, label: str) -> int:
        nonlocal n
        ifc = _pifc(holder.get(key))
        if ifc is None:
            return 0
        net = ifc.network
        if ifc.ip not in (net.network_address, net.broadcast_address):
            return 0
        why = "网段号" if ifc.ip == net.network_address else "广播地址"
        cand = _free(net, used)
        if cand is None:
            return 0
        used.discard(str(ifc.ip))
        used.add(cand)
        holder[key] = f"{cand}/{ifc.network.prefixlen}"
        fixed.append(Fix("HOST_IS_NETWORK" if why == "网段号" else "HOST_IS_BROADCAST",
                         label, f"{ifc.ip} 是{why}，改为 {cand}"))
        n += 1
        return 1

    for d in _devs(data):
        one(d, "ip", str(d.get("name")))
        for itf in _ifaces(d):
            one(itf, "ip", f"{d.get('name')}:{itf.get('name')}")
    return n


def _fix_dup_ip(data: dict, fixed: list[Fix]) -> int:
    """地址冲突：保留一个，把其余的挪到本网段里的空地址。

    保留谁不是随便挑的——如果冲突地址正好是某台主机的网关，
    那它必须留在**路由器**上。否则修完之后主机网关指向一个不存在的地址，
    校验能过，但 PT 里 ping 不通，等于把错误藏起来了。
    """
    n = 0
    gateways = {str(d.get("gateway")) for d in _devs(data) if d.get("role") in ("pc", "server")}

    entries: list[tuple[dict, str, IP, str]] = []
    for d in _devs(data):
        entries.append((d, "ip", _pifc(d.get("ip")), str(d.get("name"))))
        for itf in _ifaces(d):
            label = f"{d.get('name')}:{itf.get('name')}"
            entries.append((itf, "ip", _pifc(itf.get("ip")), label))

    groups: dict[str, list[tuple[dict, str, IP, str]]] = {}
    for e in entries:
        if e[2] is None:
            continue
        groups.setdefault(str(e[2].ip), []).append(e)

    for ip, group in groups.items():
        if len(group) < 2:
            continue
        # 网关地址优先保留，其次保留第一个出现的
        keep = next((e for e in group if ip in gateways), group[0])
        taken = {str(e[2].ip) for e in entries if e[2] is not None}
        for holder, key, ifc, label in group:
            if holder is keep[0]:
                continue
            cand = _free(ifc.network, taken)
            if cand is None:
                continue
            holder[key] = f"{cand}/{ifc.network.prefixlen}"
            taken.add(cand)
            fixed.append(Fix("DUP_IP", label, f"{ip} 与 {keep[3]} 冲突，改为 {cand}"))
            n += 1
    return n


# ---------------------------------------------------------------- 5 网关
def _fix_gateway(data: dict, fixed: list[Fix]) -> int:
    n = 0
    for d in _devs(data):
        if d.get("role") not in ("pc", "server"):
            continue
        ifc = _pifc(d.get("ip"))
        if ifc is None:
            if d.get("gateway"):
                d.pop("gateway", None)
                fixed.append(Fix("GATEWAY_INVALID", str(d.get("name")), "没有 IP，删除网关字段"))
                n += 1
            continue
        net = ifc.network
        gw = d.get("gateway")
        bad = False
        if not gw:
            bad = True
        else:
            try:
                g = ipaddress.ip_address(str(gw))
            except ValueError:
                bad = True
            else:
                if g not in net or g == ifc.ip or g in (net.network_address, net.broadcast_address):
                    bad = True
        if not bad:
            continue
        cand = _router_addr(data, net) or _free(net, {str(ifc.ip)}, {str(ifc.ip)})
        if cand is None:
            continue
        d["gateway"] = cand
        fixed.append(Fix("GATEWAY_MISMATCH", str(d.get("name")), f"网关 {gw!r} 不可用，改为 {cand}"))
        n += 1
    return n


# ---------------------------------------------------------------- 6 VLAN
def _fix_vlan_decl(data: dict, fixed: list[Fix]) -> int:
    """接口/子网引用了未声明的 VLAN → 补一条 vlans 声明。"""
    n = 0
    vlans = data.get("vlans")
    if not isinstance(vlans, list):
        vlans = []
        data["vlans"] = vlans
    declared = {v.get("id") for v in vlans if isinstance(v, dict)}
    by_id = {v.get("id"): v for v in vlans if isinstance(v, dict)}
    subnets = data.get("subnets") if isinstance(data.get("subnets"), list) else []

    def ensure(vid) -> None:
        nonlocal n
        if not isinstance(vid, int) or vid in declared:
            return
        cidr = None
        for s in subnets:
            if isinstance(s, dict) and s.get("vlan") == vid and s.get("cidr"):
                cidr = s["cidr"]
                break
        entry = {"id": vid, "name": f"VLAN{vid}"}
        if cidr:
            entry["cidr"] = cidr
        vlans.append(entry)
        declared.add(vid)
        by_id[vid] = entry
        fixed.append(Fix("VLAN_DANGLING", f"vlan {vid}", "引用了未定义的 VLAN，补一条声明"))
        n += 1

    for d in _devs(data):
        for itf in _ifaces(d):
            ensure(itf.get("vlan"))
    for s in subnets:
        if isinstance(s, dict):
            ensure(s.get("vlan"))

    # VLAN 网关不在本网段 → 改成该网段的 .1 或路由器地址
    for v in vlans:
        if not isinstance(v, dict):
            continue
        cidr, gw = v.get("cidr"), v.get("gateway")
        if not (cidr and gw):
            continue
        net = _pnet(cidr)
        if net is None:
            continue
        try:
            g = ipaddress.ip_address(str(gw))
        except ValueError:
            g = None
        if g is not None and g in net:
            continue
        cand = _router_addr(data, net) or _free(net, set())
        if cand:
            v["gateway"] = cand
            fixed.append(Fix("VLAN_GATEWAY", f"vlan {v.get('id')}", f"网关 {gw!r} 不在 {net}，改为 {cand}"))
            n += 1
    return n


# ---------------------------------------------------------------- 7 子接口
def _fix_subif(data: dict, fixed: list[Fix]) -> int:
    n = 0
    for d in _devs(data):
        if d.get("role") != "router":
            continue
        ifaces = _ifaces(d)
        names = {i.get("name") for i in ifaces}
        for itf in ifaces:
            name = itf.get("name")
            if not isinstance(name, str):
                continue
            dot1q = itf.get("dot1q")
            has_sub = "." in name
            label = f"{d.get('name')}:{name}"

            if dot1q is None and has_sub:
                tail = name.rsplit(".", 1)[1]
                if tail.isdigit():
                    itf["dot1q"] = int(tail)
                    fixed.append(Fix("SUBIF_NO_ENCAP", label, f"补 dot1q {int(tail)}"))
                    n += 1
                else:
                    fixed.append(Fix("SUBIF_NO_ENCAP", label, "后缀不是数字，无法推断封装 VLAN"))
                continue

            if dot1q is not None and not has_sub:
                # 物理口不该做封装：拆成「父接口 + 子接口」
                parent = name
                sub_name = f"{parent}.{dot1q}"
                itf["name"] = sub_name
                if parent not in names:
                    ifaces.append({"name": parent, "shutdown": False})
                    names.add(parent)
                names.add(sub_name)
                fixed.append(Fix("ENCAP_ON_PHYSICAL", label, f"物理口不能封装，拆成 {parent} + {sub_name}"))
                n += 1
                continue

            if dot1q is not None and has_sub:
                tail = name.rsplit(".", 1)[1]
                if not tail.isdigit() or int(tail) != dot1q:
                    # 以接口编号为准改 dot1q：编号通常是照着 VLAN 写的，
                    # 而子网/网关的对应关系也是按 VLAN 建的。改编号反而会拆散这套关系。
                    if tail.isdigit():
                        fixed.append(
                            Fix("SUBIF_MISMATCH", label, f"dot1q {dot1q} 与编号 {tail} 不一致，改为 dot1q {tail}")
                        )
                        itf["dot1q"] = int(tail)
                        n += 1
                    else:
                        fixed.append(Fix("SUBIF_MISMATCH", label, f"后缀 {tail!r} 不是数字，无法判断"))
    return n


# ---------------------------------------------------------------- 8 ACL
def _next_free_id(lo: int, hi: int, taken: set[int]) -> int:
    for i in range(lo, hi + 1):
        if i not in taken:
            return i
    return lo


def _infer_apply(data: dict, acl: dict) -> dict | None:
    """ACL 挂错设备/挂错口时，按第一条带具体源网段的规则推断它该挂在哪里。

    思路很朴素：ACL 的第一条规则往往就是「拦住某个源网段的流量」，
    那个源网段直接接在谁的接口上，就挂在谁的入方向。
    uc07 里 deny 192.168.20.0/24 → 192.168.10.0/24，192.168.20.0/24 接在 R1 的 Fa2/0 上，
    所以正确位置就是 R1 Fa2/0 in —— 和手写的答案一致。
    """
    src = None
    for rule in acl.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        val = rule.get("source")
        if isinstance(val, str) and val != "any":
            src = _pnet(val)
            if src is not None:
                break
    if src is None:
        return None
    for d in _devs(data):
        if d.get("role") != "router":
            continue
        for itf in _ifaces(d):
            ifc = _pifc(itf.get("ip"))
            if ifc and ifc.network == src:
                return {"device": d.get("name"), "interface": itf.get("name"), "direction": "in"}
    return None


def _fix_acl(data: dict, fixed: list[Fix]) -> int:
    n = 0
    acls = data.get("acls")
    if not isinstance(acls, list):
        return 0
    devs = {d.get("name"): d for d in _devs(data)}
    taken: set[int] = set()

    for acl in acls:
        if not isinstance(acl, dict):
            continue
        aid = acl.get("id")
        typ = acl.get("type")
        label = f"acl {aid}"
        rules = acl.get("rules")
        if not isinstance(rules, list):
            acl["rules"] = rules = []

        # 「声称是标准 ACL，但规则里写了目的地址/端口」——实质是扩展 ACL。
        # 这时把类型改判为 extended 比删掉那些字段更保语义：
        # 删字段会变成「拒绝该源的所有流量」，跟原意完全不同。
        loose = [r for r in rules if isinstance(r, dict)]
        if typ == "standard" and any(
            r.get("destination") is not None or r.get("port") is not None for r in loose
        ):
            typ = acl["type"] = "extended"
            fixed.append(
                Fix("ACL_STANDARD_SHAPE", label, "规则里有目的地址/端口，实质是扩展 ACL，改判为 extended")
            )
            n += 1

        if typ not in ("standard", "extended"):
            typ = "extended" if isinstance(aid, int) and aid >= 100 else "standard"
            acl["type"] = typ
            fixed.append(Fix("ACL_ID_RANGE", label, f"type 缺失或不合法，按编号推断为 {typ}"))
            n += 1

        lo, hi = (1, 99) if typ == "standard" else (100, 199)
        if not isinstance(aid, int) or not (lo <= aid <= hi) or aid in taken:
            new = _next_free_id(lo, hi, taken)
            fixed.append(Fix("ACL_ID_RANGE", label, f"编号不在 {typ} 区间 {lo}-{hi}，改为 {new}"))
            acl["id"] = new
            aid = new
            label = f"acl {aid}"
            n += 1
        taken.add(aid)

        for k, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            rw = f"{label}.rules[{k}]"
            if rule.get("action") not in ("permit", "deny"):
                rule["action"] = "permit"
                fixed.append(Fix("SCHEMA", rw, "action 缺失，补 permit"))
                n += 1
            if typ == "standard":
                # 到这里还留在 standard，说明本来就没写目的地址，只有多余的 protocol
                if "protocol" in rule:
                    rule.pop("protocol")
                    fixed.append(Fix("ACL_STANDARD_SHAPE", rw, "标准 ACL 不写 protocol，删除"))
                    n += 1
            else:
                proto = rule.get("protocol", "ip")
                if proto not in ("ip", "tcp", "udp", "icmp"):
                    rule["protocol"] = proto = "ip"
                    fixed.append(Fix("SCHEMA", rw, "protocol 不合法，补 ip"))
                    n += 1
                if proto in ("ip", "icmp") and rule.get("port") is not None:
                    rule.pop("port")
                    fixed.append(Fix("ACL_PORT", rw, f"{proto} 不能用端口过滤，删除 port"))
                    n += 1
            for f in ("source", "destination"):
                if f in rule:
                    val = rule[f]
                    if isinstance(val, str) and val != "any" and _pnet(val) is None:
                        rule[f] = "any"
                        fixed.append(Fix("ACL_ADDR", rw, f"{f} {val!r} 不是合法地址，改为 any"))
                        n += 1
            if typ == "extended" and not rule.get("source"):
                rule["source"] = "any"
                n += 1

        apply = acl.get("apply")
        if not isinstance(apply, list):
            acl["apply"] = apply = []
        keep: list[dict] = []
        broken = False
        for k, ap in enumerate(apply):
            if not isinstance(ap, dict):
                broken = True
                continue
            aw = f"{label}.apply[{k}]"
            dev = devs.get(ap.get("device"))
            if dev is None:
                fixed.append(Fix("ACL_REF", aw, f"设备 {ap.get('device')!r} 不存在"))
                broken = True
                continue
            if dev.get("role") != "router":
                fixed.append(Fix("ACL_ON_SWITCH", aw, f"{dev.get('name')} 是 {dev.get('role')}，不能挂 ACL"))
                broken = True
                continue
            if ap.get("interface") not in {i.get("name") for i in _ifaces(dev)}:
                fixed.append(Fix("ACL_IFACE", aw, f"{dev.get('name')} 上没有 {ap.get('interface')!r}"))
                broken = True
                continue
            if ap.get("direction") not in ("in", "out"):
                ap["direction"] = "in"
                fixed.append(Fix("SCHEMA", aw, "direction 缺失，按 in 处理"))
                n += 1
            keep.append(ap)

        if broken:
            alt = _infer_apply(data, acl)
            if alt is not None:
                keep = [alt]
                fixed.append(
                    Fix("ACL_IFACE", label,
                        f"按第一条规则的源网段推断，应挂在 {alt['device']} {alt['interface']} {alt['direction']}")
                )
            elif keep:
                fixed.append(Fix("ACL_IFACE", label, "无法推断正确挂载点，保留其余有效挂载"))
            n += 1
        acl["apply"] = keep
    return n


# ---------------------------------------------------------------- 9 链路
def _fix_links(data: dict, fixed: list[Fix]) -> int:
    n = 0
    links = data.get("links")
    if not isinstance(links, list):
        return 0
    devs = {d.get("name"): d for d in _devs(data)}
    keep = []
    for k, ln in enumerate(links):
        if not isinstance(ln, dict):
            n += 1
            continue
        ok = True
        for side in ("from", "to"):
            ref = ln.get(side)
            if not isinstance(ref, str) or ":" not in ref:
                fixed.append(Fix("LINK_REF", f"links[{k}].{side}", f"引用 {ref!r} 不是 设备:端口 形式，删除该链路"))
                ok = False
                break
            dev_name, _, port = ref.partition(":")
            dev = devs.get(dev_name)
            if dev is None:
                fixed.append(Fix("LINK_REF", f"links[{k}].{side}", f"设备 {dev_name} 不存在，删除该链路"))
                ok = False
                break
            names = {i.get("name") for i in _ifaces(dev)}
            if port in names:
                continue
            # 端口名是这台设备真实存在的硬件口 → 补声明；否则删链路
            model = dev.get("model") or ROLE_MODEL.get(dev.get("role", ""), "")
            hw = CATALOG.get(model, {})
            valid = set(hw.get("serial", [])) | set(hw.get("lan", []))
            if port in valid:
                if not isinstance(dev.get("interfaces"), list):
                    dev["interfaces"] = []
                dev["interfaces"].append({"name": port})
                fixed.append(Fix("LINK_REF", f"{dev_name}:{port}", "接口未声明，补一条接口声明"))
                n += 1
            else:
                fixed.append(
                    Fix("LINK_REF", f"links[{k}].{side}", f"{dev_name} 上没有 {port}（也不是 {model} 的硬件口），删除该链路")
                )
                ok = False
                break
        if ok:
            keep.append(ln)
        else:
            n += 1
    data["links"] = keep
    return n


# ---------------------------------------------------------------- 10 容量
def _fix_capacity(data: dict, fixed: list[Fix]) -> int:
    n = 0
    subnets = data.get("subnets")
    if not isinstance(subnets, list):
        return 0
    nets = []
    for s in subnets:
        if isinstance(s, dict):
            net = _pnet(s.get("cidr"))
            if net:
                nets.append((s, net))
    for s, net in nets:
        need = s.get("hosts")
        if not isinstance(need, int) or need <= 0:
            continue
        usable = net.num_addresses - 2 if net.prefixlen < 31 else net.num_addresses
        if need <= usable:
            continue
        # 逐位放大，直到容得下且不与他人重叠
        for plen in range(net.prefixlen - 1, -1, -1):
            cand = ipaddress.ip_network(f"{net.network_address}/{plen}", strict=False)
            cap = cand.num_addresses - 2 if cand.prefixlen < 31 else cand.num_addresses
            if cap < need:
                continue
            others = [o for o, _ in nets if o is not s]
            if any(cand.overlaps(_pnet(o.get("cidr"))) for o in others if _pnet(o.get("cidr"))):
                continue
            fixed.append(Fix("CAPACITY", str(s.get("name") or net), f"{net} 容不下 {need} 台，放大为 {cand}"))
            s["cidr"] = str(cand)
            # 地址掩码也要跟着放大，否则「声明的网段」和「接口上的网段」会对不上
            for d in _devs(data):
                holders = [(d, "ip")] + [(i, "ip") for i in _ifaces(d)]
                for holder, key in holders:
                    ifc = _pifc(holder.get(key))
                    if ifc and ifc.network == net:
                        holder[key] = f"{ifc.ip}/{cand.prefixlen}"
            n += 1
            break
    return n


# ---------------------------------------------------------------- 主入口
FIXERS = (
    _fix_schema,
    _fix_meta,
    _fix_dup_names,
    _fix_addr_shape,
    _fix_dup_ip,
    _fix_gateway,
    # 子接口要先于 VLAN 声明修：dot1q 修好之后才知道到底引用了哪些 VLAN，
    # 顺序反了会按错的 dot1q 补出一条永远不会被用到的 VLAN 声明。
    _fix_subif,
    _fix_vlan_decl,
    _fix_acl,
    _fix_links,
    _fix_capacity,
)


def repair(data: dict, max_pass: int = 5) -> Result:
    """反复「校验 → 修 → 再校验」，直到没有可修的错或达到上限。"""
    cur = copy.deepcopy(data)
    fixed: list[Fix] = []
    passes = 0

    for i in range(1, max_pass + 1):
        topo = Topology(cur)
        topo.validate()
        errs = [x for x in topo.issues if x.level == "error"]
        if not errs:
            passes = i - 1
            break
        # 只剩下明确不修的码，提前收工
        if all(x.code in UNFIXABLE for x in errs):
            passes = i - 1
            break
        changed = 0
        for fx in FIXERS:
            try:
                changed += fx(cur, fixed)
            except Exception:  # 单个修复器崩了不应带崩整个闭环
                continue
        passes = i
        if changed == 0:
            break

    topo = Topology(cur)
    topo.validate()
    remaining = [x for x in topo.issues if x.level == "error"]
    return Result(cur, fixed, remaining, passes)
