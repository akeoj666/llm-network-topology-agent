"""故障注入目录：往一份合法拓扑里精确注入一类错误，用来验证闭环。

每条故障都声明「注入之后，校验器应该报哪个码」。
C3 的闭环断言就是：注入 → 报对码 → 修好 → 再校验通过。

跟 `tools/test_accept.py` 的区别：
  那个测的是**验收器**（能不能看出拓扑不对），
  这个测的是**校验器 + 修复器**（能不能定位到字段、能不能改回合法）。

有些故障是**故意不可修的**（子网重叠、掩码非法）：怎么挪没有唯一答案，
修它等于替用户做决定。这类故障的期望是「报告出来但不乱改」——
宁可停在错误上，也不要静默产出一个看起来正常、但跟需求不符的拓扑。
"""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass
from typing import Callable

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def load(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / f"{name}.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 定位小工具
def dev(data: dict, name: str) -> dict:
    return next(d for d in data["devices"] if d["name"] == name)


def port(data: dict, name: str, ifname: str) -> dict:
    return next(i for i in dev(data, name)["interfaces"] if i["name"] == ifname)


def by_role(data: dict, role: str) -> dict:
    return next(d for d in data["devices"] if d["role"] == role)


def link_idx(data: dict, a: str, b: str) -> int:
    for i, ln in enumerate(data["links"]):
        if {ln["from"].split(":")[0], ln["to"].split(":")[0]} == {a, b}:
            return i
    raise KeyError(f"{a}-{b}")


@dataclass
class Fault:
    code: str          # 期望校验器报出的错误码
    key: str           # 唯一标识
    desc: str          # 人话描述
    base: str          # 注入到哪个基线用例
    apply: Callable[[dict], None]
    fixable: bool = True   # 修复器能不能确定性地修到「校验通过」
    strict: bool = True    # 修完之后语义是否也该保持（accept 的断言仍然全过）

    def inject(self) -> dict:
        data = copy.deepcopy(load(self.base))
        self.apply(data)
        return data


# ---------------------------------------------------------------- 地址类
def _gw_offnet(d: dict) -> None:
    d["devices"][-1]["gateway"] = "192.168.99.1" if "192.168" in d["devices"][-1]["ip"] else "10.99.99.1"


def _host_is_network(d: dict) -> None:
    # R2 的 Fa2/0 本来是 shutdown 的，这里给它一个网络号地址
    port(d, "R2", "FastEthernet2/0").update({"ip": "172.20.5.0/24", "shutdown": False})


def _host_is_bcast(d: dict) -> None:
    port(d, "R2", "FastEthernet2/0").update({"ip": "172.20.5.255/24", "shutdown": False})


def _dup_ip(d: dict) -> None:
    # 两台主机撞了同一个地址——模型最常见的一类编址错误
    dev(d, "PC1")["ip"] = dev(d, "PC0")["ip"]


def _subnet_overlap(d: dict) -> None:
    d["subnets"].append({"cidr": "192.168.16.0/25", "name": "重叠段"})


def _bad_mask(d: dict) -> None:
    d["subnets"][0]["cidr"] = "192.168.16.0/33"


def _capacity(d: dict) -> None:
    next(s for s in d["subnets"] if s.get("name") == "dept-c")["hosts"] = 500  # /24 只有 254


# ---------------------------------------------------------------- 结构类
def _dup_device(d: dict) -> None:
    d["devices"].append(copy.deepcopy(dev(d, "PC1")))


def _bad_meta(d: dict) -> None:
    d["meta"]["id"] = "UC-9A"          # 不符合 ^UC-\d{2}$


def _missing_meta_id(d: dict) -> None:
    d["meta"].pop("id", None)


def _hallucinated_field(d: dict) -> None:
    d["devices"][0]["priority"] = "high"
    d["devices"][0]["interfaces"][0]["duplex"] = "full"
    d["meta"]["author"] = "gpt"


# ---------------------------------------------------------------- VLAN 类
def _vlan_dangling(d: dict) -> None:
    d.pop("vlans")


def _subif_no_encap(d: dict) -> None:
    for itf in dev(d, "R1")["interfaces"]:
        itf.pop("dot1q", None)


def _subif_mismatch(d: dict) -> None:
    for itf in dev(d, "R1")["interfaces"]:
        if itf["name"].endswith(".20"):
            itf["dot1q"] = 30


def _encap_on_physical(d: dict) -> None:
    """把 dot1q 挪到物理口上，删掉子接口——模型常见的「忘了拆子接口」。"""
    r1 = dev(d, "R1")
    keep = []
    for itf in r1["interfaces"]:
        if "." in itf["name"]:
            continue
        keep.append(itf)
    r1["interfaces"] = keep
    port(d, "R1", "FastEthernet2/0").update({"ip": "192.168.10.1/24", "dot1q": 10})


# ---------------------------------------------------------------- ACL 类
def _acl_id_range(d: dict) -> None:
    d["acls"][0]["id"] = 50           # 扩展 ACL 用了标准 ACL 的编号


def _acl_std_shape(d: dict) -> None:
    d["acls"][0].update(
        {
            "id": 10,
            "type": "standard",
            "rules": [
                {"action": "deny", "protocol": "ip",
                 "source": "192.168.30.0/24", "destination": "192.168.40.0/24"},
                {"action": "permit", "source": "any"},
            ],
        }
    )


def _acl_port_on_ip(d: dict) -> None:
    d["acls"][0]["rules"][0]["port"] = 80


def _acl_on_switch(d: dict) -> None:
    d["acls"][0]["apply"][0].update({"device": "SW0", "interface": "FastEthernet0/1"})


def _acl_bad_iface(d: dict) -> None:
    d["acls"][0]["apply"][0]["interface"] = "FastEthernet0/0"


def _acl_bad_addr(d: dict) -> None:
    d["acls"][0]["rules"][0]["source"] = "192.168.30.999/24"


# ---------------------------------------------------------------- 链路类
def _link_bad_device(d: dict) -> None:
    d["links"][0]["to"] = "R9:Serial1/0"


def _link_bad_port(d: dict) -> None:
    # Router-PT-Empty 上没有 FastEthernet0/0 —— 模型最常犯的错
    d["links"][0]["to"] = "R2:FastEthernet0/0"


def _link_undeclared_port(d: dict) -> None:
    """端口是真实硬件口但没在 interfaces 里声明 —— 可以补声明。"""
    d["links"].append({"from": "SW0:FastEthernet0/2", "to": "R2:FastEthernet3/0", "cable": "copper"})


CATALOG: list[Fault] = [
    # 地址
    Fault("GATEWAY_MISMATCH", "gw_offnet", "主机网关跑到别的网段", "uc01", _gw_offnet),
    Fault("HOST_IS_NETWORK", "host_is_network", "接口配了网段号", "uc01", _host_is_network),
    Fault("HOST_IS_BROADCAST", "host_is_broadcast", "接口配了广播地址", "uc01", _host_is_bcast),
    Fault("DUP_IP", "dup_ip", "两台设备地址冲突", "uc01", _dup_ip),
    Fault("SUBNET_OVERLAP", "subnet_overlap", "两个子网互相包含", "uc01", _subnet_overlap, fixable=False),
    Fault("MASK_INVALID", "bad_mask", "掩码写成 /33", "uc01", _bad_mask, fixable=False),
    Fault("CAPACITY", "capacity", "子网容不下声明的主机数", "uc02", _capacity),
    # 结构
    Fault("DUP_DEVICE", "dup_device", "设备重名", "uc01", _dup_device),
    Fault("SCHEMA", "bad_meta_id", "meta.id 不符合 UC-数字两位", "uc01", _bad_meta),
    Fault("META", "missing_meta_id", "meta.id 整个缺失", "uc01", _missing_meta_id),
    Fault("SCHEMA", "hallucinated_field", "编造了 DSL 里没有的字段", "uc01", _hallucinated_field),
    # VLAN
    Fault("VLAN_DANGLING", "vlan_dangling", "接口引用了未声明的 VLAN", "uc05", _vlan_dangling),
    Fault("SUBIF_NO_ENCAP", "subif_no_encap", "子接口漏了 dot1q 封装", "uc05", _subif_no_encap),
    Fault("SUBIF_MISMATCH", "subif_mismatch", "子接口编号与 dot1q 不一致", "uc05", _subif_mismatch),
    Fault("ENCAP_ON_PHYSICAL", "encap_on_physical", "把 dot1q 写在物理口上", "uc05", _encap_on_physical,
          strict=False),   # 注进去时连 VLAN20 的子接口一起删了，修不回来
    # ACL
    Fault("ACL_ID_RANGE", "acl_id_range", "扩展 ACL 用了 1-99 编号", "uc07", _acl_id_range),
    Fault("ACL_STANDARD_SHAPE", "acl_std_shape", "标准 ACL 里写了目的地址", "uc07", _acl_std_shape),
    Fault("ACL_PORT", "acl_port_on_ip", "ip 协议规则带了端口", "uc07", _acl_port_on_ip),
    Fault("ACL_ON_SWITCH", "acl_on_switch", "ACL 挂到了交换机上", "uc07", _acl_on_switch),
    Fault("ACL_IFACE", "acl_bad_iface", "ACL 挂到了不存在的接口", "uc07", _acl_bad_iface),
    Fault("ACL_ADDR", "acl_bad_addr", "ACL 里写了非法地址", "uc07", _acl_bad_addr,
          strict=False),   # 非法地址推不出原意，只能降级成 any
    # 链路
    Fault("LINK_REF", "link_bad_device", "链路指向不存在的设备", "uc01", _link_bad_device),
    Fault("LINK_REF", "link_bad_port", "链路用了 Router-PT-Empty 没有的 Fa0/0", "uc01", _link_bad_port),
    Fault("LINK_REF", "link_undeclared_port", "端口真实存在但没在 interfaces 里声明", "uc01", _link_undeclared_port),
]

BY_KEY = {f.key: f for f in CATALOG}


def all_faults() -> list[Fault]:
    return list(CATALOG)
