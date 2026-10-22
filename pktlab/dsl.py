"""DSL 加载与语义校验。

对应评分标准 A1（schema 校验）与 A2（编址冲突检测）：
  - 语法层：JSON Schema 严格校验（additionalProperties=false，防止 LLM 幻觉字段）
  - 语义层：地址重叠 / 网关错配 / 掩码非法 / VLAN 悬空 / 容量不足 / 链路引用
每条错误都带 code + 可读中文描述，可直接回灌给 LLM 做自修复。
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "topology.schema.json"


def _fit_prefix(need: int) -> int:
    """容纳 need 台主机所需的最长可用前缀长度。"""
    prefix = 32
    while prefix > 0 and (2 ** (32 - prefix) - 2) < need:
        prefix -= 1
    return prefix


@dataclass
class Issue:
    level: str  # error | warning
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        tag = "错误" if self.level == "error" else "警告"
        loc = f" @{self.where}" if self.where else ""
        return f"[{tag}/{self.code}]{loc} {self.message}"


@dataclass
class Topology:
    data: dict
    issues: list[Issue] = field(default_factory=list)

    # ---------- 载入 ----------
    @classmethod
    def load(cls, path: str | Path) -> "Topology":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(raw)

    @property
    def meta(self) -> dict:
        return self.data.get("meta", {})

    @property
    def devices(self) -> list[dict]:
        return self.data.get("devices", [])

    @property
    def links(self) -> list[dict]:
        return self.data.get("links", [])

    def device(self, name: str) -> dict | None:
        for d in self.devices:
            if d["name"] == name:
                return d
        return None

    # ---------- 校验 ----------
    def validate(self) -> list[Issue]:
        self.issues = []
        self._check_schema()
        self._check_names()
        self._check_pool()
        self._check_addressing()
        self._check_gateways()
        self._check_vlans()
        self._check_subinterfaces()
        self._check_acls()
        self._check_capacity()
        self._check_links()
        return self.issues

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    def report(self) -> str:
        if not self.issues:
            return "校验通过：0 错误 0 警告"
        lines = [str(i) for i in self.issues]
        n_err = sum(1 for i in self.issues if i.level == "error")
        lines.append(f"合计 {n_err} 错误 / {len(self.issues) - n_err} 警告")
        return "\n".join(lines)

    # ---------- 各项检查 ----------
    def _check_schema(self) -> None:
        if jsonschema is None:
            self.issues.append(Issue("warning", "SCHEMA_UNAVAILABLE", "jsonschema 未安装，跳过语法校验"))
            return
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        for err in sorted(validator.iter_errors(self.data), key=lambda e: list(e.path)):
            path = "/".join(str(p) for p in err.path) or "<根>"
            self.issues.append(Issue("error", "SCHEMA", err.message, path))

    def _check_names(self) -> None:
        seen = set()
        for d in self.devices:
            n = d.get("name", "")
            if n in seen:
                self.issues.append(Issue("error", "DUP_DEVICE", f"设备名重复：{n}", n))
            seen.add(n)
        if self.meta.get("id") is None:
            self.issues.append(Issue("error", "META", "缺少 meta.id"))

    def _parse(self, text: str, where: str) -> ipaddress.IPv4Interface | None:
        try:
            return ipaddress.ip_interface(text)
        except ValueError as exc:
            self.issues.append(Issue("error", "MASK_INVALID", f"无法解析地址 {text!r}：{exc}", where))
            return None

    def iface_table(self) -> list[tuple[str, str, ipaddress.IPv4Interface]]:
        out = []
        for d in self.devices:
            for itf in d.get("interfaces", []):
                if itf.get("ip"):
                    parsed = ipaddress.ip_interface(itf["ip"])
                    out.append((d["name"], itf["name"], parsed))
            if d.get("ip"):
                out.append((d["name"], "<host>", ipaddress.ip_interface(d["ip"])))
        return out

    def _check_pool(self) -> None:
        """所有子网必须落在声明的地址池内，且总容量不得超池。"""
        pool_text = self.data.get("pool")
        if not pool_text:
            return
        try:
            pool = ipaddress.ip_network(pool_text, strict=False)
        except ValueError as exc:
            self.issues.append(Issue("error", "POOL_INVALID", f"地址池 {pool_text!r} 不合法：{exc}", "pool"))
            return
        need = 0
        for s in self.data.get("subnets", []):
            try:
                net = ipaddress.ip_network(s["cidr"], strict=False)
            except ValueError:
                continue
            need += net.num_addresses
            if not (net.subnet_of(pool) if hasattr(net, "subnet_of") else False):
                self.issues.append(
                    Issue(
                        "error",
                        "POOL_OVERFLOW",
                        f"子网 {net}（{s.get('name', '')}）超出可用地址池 {pool}，"
                        f"池内最大只能划到 /{pool.prefixlen}",
                        s.get("name", ""),
                    )
                )
        if need > pool.num_addresses:
            self.issues.append(
                Issue(
                    "error",
                    "POOL_CAPACITY",
                    f"共需 {need} 个地址，地址池 {pool} 只有 {pool.num_addresses} 个，差 {need - pool.num_addresses} 个",
                    "pool",
                )
            )

    def _check_addressing(self) -> None:
        seen_ip: dict[str, str] = {}
        for d in self.devices:
            # 主机的地址也要进冲突表——「两台 PC 配了同一个 IP」是最常见的一类错，
            # 只查接口地址会漏掉它。
            targets: list[tuple[str, dict]] = []
            if d.get("ip"):
                targets.append(("<host>", d))
            for itf in d.get("interfaces", []):
                if itf.get("ip"):
                    targets.append((itf["name"], itf))
            for name, holder in targets:
                w = f"{d['name']}:{name}" if name != "<host>" else d["name"]
                ifc = self._parse(holder["ip"], w)
                if ifc is None:
                    continue
                if ifc.ip.version != 4:
                    continue
                key = str(ifc.ip)
                if key in seen_ip:
                    self.issues.append(
                        Issue("error", "DUP_IP", f"地址冲突：{key} 同时出现在 {seen_ip[key]} 与 {w}", w)
                    )
                seen_ip[key] = w
                if ifc.ip == ifc.network.network_address:
                    self.issues.append(
                        Issue("error", "HOST_IS_NETWORK", f"{key} 是网段号，不能配在接口上", w)
                    )
                if ifc.ip == ifc.network.broadcast_address:
                    self.issues.append(
                        Issue("error", "HOST_IS_BROADCAST", f"{key} 是广播地址，不能配在接口上", w)
                    )
        # 声明的子网之间不得重叠
        declared = []
        for s in self.data.get("subnets", []):
            try:
                declared.append((s["cidr"], ipaddress.ip_network(s["cidr"], strict=False), s.get("name", "")))
            except ValueError as exc:
                self.issues.append(Issue("error", "MASK_INVALID", f"子网 {s.get('cidr')!r} 不合法：{exc}", s.get("name", "")))
        for i in range(len(declared)):
            for j in range(i + 1, len(declared)):
                a, b = declared[i][1], declared[j][1]
                if a.version == b.version and a.overlaps(b):
                    self.issues.append(
                        Issue(
                            "error",
                            "SUBNET_OVERLAP",
                            f"子网重叠：{declared[i][0]}({declared[i][2]}) 与 {declared[j][0]}({declared[j][2]}) 相交",
                            declared[i][2] or declared[j][2],
                        )
                    )

    def _check_gateways(self) -> None:
        hosts_by_net: dict[str, set[str]] = {}
        for d in self.devices:
            if not d.get("ip"):
                continue
            try:
                ifc = ipaddress.ip_interface(d["ip"])
            except ValueError:
                continue
            hosts_by_net.setdefault(str(ifc.network), set()).add(str(ifc.ip))

        for d in self.devices:
            if not d.get("gateway"):
                continue
            w = d["name"]
            try:
                gw = ipaddress.ip_address(d["gateway"])
            except ValueError:
                self.issues.append(Issue("error", "GATEWAY_INVALID", f"网关地址 {d['gateway']!r} 不合法", w))
                continue
            if not d.get("ip"):
                self.issues.append(Issue("error", "GATEWAY_INVALID", "配了网关却没有 IP", w))
                continue
            ifc = ipaddress.ip_interface(d["ip"])
            if gw not in ifc.network:
                self.issues.append(
                    Issue(
                        "error",
                        "GATEWAY_MISMATCH",
                        f"网关 {gw} 不在本机网段 {ifc.network} 内",
                        w,
                    )
                )
            elif gw == ifc.ip:
                self.issues.append(Issue("error", "GATEWAY_MISMATCH", f"网关与本机地址相同（{gw}）", w))
            elif gw in (ifc.network.network_address, ifc.network.broadcast_address):
                self.issues.append(
                    Issue("error", "GATEWAY_MISMATCH", f"网关 {gw} 是网段号或广播地址，不可用", w)
                )

    def _check_vlans(self) -> None:
        declared = {v["id"] for v in self.data.get("vlans", [])}
        for d in self.devices:
            for itf in d.get("interfaces", []):
                v = itf.get("vlan")
                if v is not None and v not in declared:
                    self.issues.append(
                        Issue(
                            "error",
                            "VLAN_DANGLING",
                            f"接口引用了未定义的 VLAN {v}（已定义：{sorted(declared) or '无'}）",
                            f"{d['name']}:{itf['name']}",
                        )
                    )
        for s in self.data.get("subnets", []):
            v = s.get("vlan")
            if v is not None and v not in declared:
                self.issues.append(
                    Issue("error", "VLAN_DANGLING", f"子网引用了未定义的 VLAN {v}", s.get("name", ""))
                )

        # VLAN 的 SVI 地址必须落在自己声明的子网里
        for v in self.data.get("vlans", []):
            gw, cidr = v.get("gateway"), v.get("cidr")
            if not (gw and cidr):
                continue
            try:
                ifc = ipaddress.ip_interface(f"{gw}/{cidr.split('/')[1]}")
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                self.issues.append(Issue("error", "VLAN_GATEWAY", f"VLAN {v['id']} 的地址不合法：{exc}", f"vlan {v['id']}"))
                continue
            if ifc.ip not in net:
                self.issues.append(
                    Issue("error", "VLAN_GATEWAY", f"VLAN {v['id']} 网关 {gw} 不在 {net} 内", f"vlan {v['id']}")
                )

    def _check_subinterfaces(self) -> None:
        """单臂路由的子接口：名字必须是 父接口.VLAN，且 dot1q 要和编号一致。"""
        for d in self.devices:
            for itf in d.get("interfaces", []):
                name, dot1q = itf["name"], itf.get("dot1q")
                has_sub = "." in name
                if dot1q is None:
                    if has_sub:
                        self.issues.append(
                            Issue(
                                "error",
                                "SUBIF_NO_ENCAP",
                                f"子接口 {name} 缺少 dot1q 封装，PT 回放会中止",
                                f"{d['name']}:{name}",
                            )
                        )
                    continue
                if not has_sub:
                    self.issues.append(
                        Issue(
                            "error",
                            "ENCAP_ON_PHYSICAL",
                            f"{name} 是物理接口，不能做 dot1Q 封装（应写成 {name}.{dot1q}）",
                            f"{d['name']}:{name}",
                        )
                    )
                    continue
                suffix = name.rsplit(".", 1)[1]
                if not suffix.isdigit() or int(suffix) != dot1q:
                    self.issues.append(
                        Issue(
                            "error",
                            "SUBIF_MISMATCH",
                            f"子接口编号 {suffix} 与 dot1q {dot1q} 不一致",
                            f"{d['name']}:{name}",
                        )
                    )

    def _check_acls(self) -> None:
        """ACL 的编号区间、引用完整性，以及规则本身的合法性。

        编号区间不是洁癖：PT 里 `access-list 100 permit ...` 如果被当成标准 ACL
        解析，后面带源/目的两个地址的写法就会整行报错，而回放是一报错就中止。
        """
        declared: dict[int, str] = {}
        for acl in self.data.get("acls", []):
            aid, kind = acl.get("id"), acl.get("type")
            where = f"acl {aid}"
            if aid is not None and kind:
                if kind == "standard" and not (1 <= aid <= 99):
                    self.issues.append(
                        Issue("error", "ACL_ID_RANGE", f"标准 ACL 编号应在 1-99，当前 {aid}", where)
                    )
                if kind == "extended" and not (100 <= aid <= 199):
                    self.issues.append(
                        Issue("error", "ACL_ID_RANGE", f"扩展 ACL 编号应在 100-199，当前 {aid}", where)
                    )
                if aid in declared:
                    self.issues.append(Issue("error", "ACL_DUP_ID", f"ACL 编号 {aid} 重复定义", where))
                declared[aid] = kind

            for j, rule in enumerate(acl.get("rules", [])):
                rw = f"{where}.rules[{j}]"
                proto = rule.get("protocol", "ip")
                if rule.get("port") is not None and proto not in ("tcp", "udp"):
                    self.issues.append(
                        Issue("error", "ACL_PORT", f"{proto} 协议不能用端口过滤", rw)
                    )
                if kind == "standard" and (rule.get("destination") or rule.get("protocol")):
                    self.issues.append(
                        Issue(
                            "error",
                            "ACL_STANDARD_SHAPE",
                            "标准 ACL 只能写源地址，不要写 protocol / destination",
                            rw,
                        )
                    )
                for field_name in ("source", "destination"):
                    val = rule.get(field_name)
                    if not val or val == "any":
                        continue
                    try:
                        ipaddress.ip_network(val, strict=False)
                    except ValueError:
                        self.issues.append(
                            Issue("error", "ACL_ADDR", f"{field_name} 地址 {val!r} 不合法", rw)
                        )

        for acl in self.data.get("acls", []):
            if not acl.get("apply"):
                self.issues.append(
                    Issue(
                        "warning",
                        "ACL_UNUSED",
                        f"ACL {acl.get('id')} 定义了但没有挂到任何接口上，不会生效",
                        f"acl {acl.get('id')}",
                    )
                )
            for k, ap in enumerate(acl.get("apply", [])):
                aw = f"acl {acl.get('id')}.apply[{k}]"
                dev = self.device(ap.get("device", ""))
                if dev is None:
                    self.issues.append(
                        Issue("error", "ACL_REF", f"引用了不存在的设备 {ap.get('device')}", aw)
                    )
                    continue
                if acl.get("id") not in declared:
                    self.issues.append(
                        Issue("error", "ACL_REF", f"ACL {acl.get('id')} 没有定义", aw)
                    )
                if dev.get("role") != "router":
                    self.issues.append(
                        Issue(
                            "error",
                            "ACL_ON_SWITCH",
                            f"ACL 目前只支持挂在路由器的三层接口上，{dev['name']} 是 {dev['role']}",
                            aw,
                        )
                    )
                names = {x["name"] for x in dev.get("interfaces", [])}
                if ap.get("interface") not in names:
                    self.issues.append(
                        Issue(
                            "error",
                            "ACL_IFACE",
                            f"{dev['name']} 上没有接口 {ap.get('interface')}（可用：{sorted(names)}）",
                            aw,
                        )
                    )

    def _check_capacity(self) -> None:
        for s in self.data.get("subnets", []):
            need = s.get("hosts")
            if not need:
                continue
            try:
                net = ipaddress.ip_network(s["cidr"], strict=False)
            except ValueError:
                continue
            usable = net.num_addresses - 2 if net.prefixlen < 31 else net.num_addresses
            if need > usable:
                self.issues.append(
                    Issue(
                        "error",
                        "CAPACITY",
                        f"{net} 只有 {usable} 个可用地址，容不下 {need} 台主机（至少需要 /{_fit_prefix(need)}）",
                        s.get("name", ""),
                    )
                )

    def _check_links(self) -> None:
        used: set[str] = set()
        seen_pairs: set[tuple[str, str]] = set()
        for i, ln in enumerate(self.links):
            ends = []
            for side in ("from", "to"):
                ref = ln.get(side, "")
                dev, _, itf = ref.partition(":")
                d = self.device(dev)
                if d is None:
                    self.issues.append(Issue("error", "LINK_REF", f"链路引用了不存在的设备 {dev}", f"links[{i}].{side}"))
                    continue
                names = {x["name"] for x in d.get("interfaces", [])}
                if names and itf not in names:
                    self.issues.append(
                        Issue("error", "LINK_REF", f"{dev} 上没有接口 {itf}（可用：{sorted(names)}）", f"links[{i}].{side}")
                    )
                used.add(dev)
                ends.append(ref)
            if len(ends) == 2:
                pair = tuple(sorted(ends))
                if pair in seen_pairs:
                    self.issues.append(Issue("warning", "LINK_DUP", f"重复链路 {pair}", f"links[{i}]"))
                seen_pairs.add(pair)

        for d in self.devices:
            if d.get("role") in ("router", "pc", "server") and d["name"] not in used:
                self.issues.append(Issue("warning", "ORPHAN", "设备没有连接任何链路", d["name"]))
