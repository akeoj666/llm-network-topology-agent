"""DSL → .pkt 组装器。

流程：
  1. 打开骨架文件，先把链路模板抄下来
  2. 清空全部链路（拓扑一律按 DSL 重建，不依赖骨架原有连线）
  3. 按角色把 DSL 设备映射到骨架的设备槽位
  4. 删除用不到的设备 → 给保留的设备改名
  5. 按 DSL 逐条新建链路（含 DCE 标记）
  6. 注入 IOS 配置 / 主机网卡参数
  7. 按统一布局摆放坐标
  8. 清掉种子自带的命令历史，编码写盘
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config, layout
from .dsl import Topology
from .pkt import Pkt

SEED = Path(__file__).resolve().parents[1] / "seeds" / "base_rip_3r3sw6pc.pkt"


class AssemblyError(RuntimeError):
    pass


def _natkey(name: str) -> tuple[str, int]:
    m = re.match(r"([A-Za-z]+)(\d+)$", name)
    return (m.group(1), int(m.group(2))) if m else (name, 0)


def _slots(pkt: Pkt) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"router": [], "switch": [], "pc": []}
    for d in pkt.devices():
        if d.model.startswith("Router"):
            out["router"].append(d.name)
        elif d.model.startswith("2960"):
            out["switch"].append(d.name)
        elif d.model == "PC-PT":
            out["pc"].append(d.name)
    return {k: sorted(v, key=_natkey) for k, v in out.items()}


def _check_port(dev_model: str, port: str, cable: str) -> None:
    cat = config.CATALOG.get(dev_model)
    if cat is None:
        return
    allowed = cat.get("serial", []) if cable == "serial" else cat.get("lan", [])
    if port not in allowed:
        raise AssemblyError(
            f"{dev_model} 上没有端口 {port}（{'串口' if cable == 'serial' else '以太网口'}可用：{allowed}）"
        )


# MDI-X 设备（交换机的普通口）。其余 PC / 服务器 / 路由器的以太口都是 MDI。
_MDIX_ROLES = {"switch"}


def effective_cable(topo: Topology, ln: dict) -> str:
    """按两端设备角色推导线型，不信模型写的 cable。

    MDI 与 MDI-X 相接用直通线，同类相接用交叉线：
      PC/路由器 → 交换机   直通
      PC → 路由器          交叉
      交换机 → 交换机      交叉
      PC → PC              交叉

    Packet Tracer 对线型基本宽容，但报告和答辩里会被问到，
    而且写错了在真实设备上就是不通，所以这里按物理规则算准。
    """
    if ln.get("cable") == "serial":
        return "serial"
    a = topo.device(ln["from"].partition(":")[0])
    b = topo.device(ln["to"].partition(":")[0])
    if a is None or b is None:
        return "copper"
    same_class = (a["role"] in _MDIX_ROLES) == (b["role"] in _MDIX_ROLES)
    return "cross" if same_class else "copper"


@dataclass
class Result:
    pkt_path: Path
    configs: dict[str, list[str]] = field(default_factory=dict)
    mapping: dict[str, str] = field(default_factory=dict)
    positions: dict[str, tuple[int, int]] = field(default_factory=dict)


def build(topo: Topology, out_path: str | Path, seed: Path | None = None) -> Result:
    topo.validate()
    if not topo.ok:
        raise AssemblyError("DSL 校验未通过：\n" + topo.report())

    # 用例可以自带骨架（设备多的实验要用扩容过的种子）
    if seed is None:
        spec = topo.data.get("seed")
        seed = (Path(__file__).resolve().parents[1] / spec) if spec else SEED
        if not Path(seed).exists():
            raise AssemblyError(f"用例指定的骨架不存在：{seed}")

    pkt = Pkt.open(seed)
    templates = {
        "serial": next(ln for ln in pkt.links() if "<TYPE>eSerial</TYPE>" in ln),
        "copper": next(ln for ln in pkt.links() if "eStraightThrough" in ln),
    }
    templates["cross"] = templates["copper"]
    pkt.clear_links()

    # ---- 映射槽位 ----
    slots = _slots(pkt)
    mapping: dict[str, str] = {}
    used = {"router": 0, "switch": 0, "pc": 0}
    for d in topo.devices:
        role = "pc" if d["role"] in ("pc", "server") else d["role"]
        if used[role] >= len(slots[role]):
            raise AssemblyError(
                f"骨架只有 {len(slots[role])} 个 {role} 槽位，DSL 需要 {used[role] + 1} 个"
            )
        mapping[d["name"]] = slots[role][used[role]]
        used[role] += 1

    # ---- 删除多余设备 ----
    keep = set(mapping.values())
    for name in [d.name for d in pkt.devices()]:
        if name not in keep:
            pkt.remove_device(name)

    # ---- 改名 ----
    for dsl_name, sk_name in mapping.items():
        pkt.by_name(sk_name).rename(dsl_name)

    # ---- 建链路 ----
    for ln in topo.links:
        a_dev, _, a_port = ln["from"].partition(":")
        b_dev, _, b_port = ln["to"].partition(":")
        cable = effective_cable(topo, ln)
        for dev, port in ((a_dev, a_port), (b_dev, b_port)):
            if topo.device(dev) is None:
                raise AssemblyError(f"链路引用了不存在的设备 {dev}")
            _check_port(pkt.by_name(dev).model, port, cable)
        pkt.add_link(
            (a_dev, a_port),
            (b_dev, b_port),
            cable=cable,
            dce_side=ln.get("dce", "b"),
            template=templates[cable],
        )

    # ---- 注入配置 ----
    configs = config.generate_all(topo)
    for d in topo.devices:
        dev = pkt.by_name(d["name"])
        lines = configs.get(d["name"], [])
        if lines:
            # 先清掉骨架残留的端口地址，否则配置里的同名地址会被判冲突而拒绝
            dev.clear_port_ips()
            dev.set_config(lines)
        if d["role"] in ("pc", "server") and d.get("ip"):
            import ipaddress

            ifc = ipaddress.ip_interface(d["ip"])
            dev.set_host_ip(str(ifc.ip), str(ifc.netmask), d.get("gateway", ""))

    # ---- VLAN 数据库（不是配置行，要写进设备的 <VLANS> 节点）----
    vlans = [(v["id"], v["name"]) for v in topo.data.get("vlans", [])]
    if vlans:
        for d in topo.devices:
            if d["role"] == "switch":
                pkt.set_vlans(d["name"], vlans)

    # ---- 布局 ----
    positions = layout.to_pt(layout.compute(topo))
    for name, (x, y) in positions.items():
        try:
            pkt.by_name(name).move(x, y)
        except KeyError:
            pass

    # ---- 画布便签：链路网段 + 主机地址，按新坐标重写 ----
    notes: list[tuple[str, float, float]] = []
    for ln in topo.links:
        if not ln.get("subnet"):
            continue
        a_dev = ln["from"].partition(":")[0]
        b_dev = ln["to"].partition(":")[0]
        if a_dev in positions and b_dev in positions:
            ax, ay = positions[a_dev]
            bx, by = positions[b_dev]
            notes.append((ln["subnet"], (ax + bx) / 2, (ay + by) / 2 - 26))
    for d in topo.devices:
        if d["role"] in ("pc", "server") and d.get("ip") and d["name"] in positions:
            x, y = positions[d["name"]]
            notes.append((str(ipaddress.ip_interface(d["ip"]).ip), x, y + 52))

    # VLAN 网段没有三层链路可挂，单独标在做 trunk 的交换机上方
    vlan_rows = [v for v in topo.data.get("vlans", []) if v.get("cidr")]
    if vlan_rows:
        anchor = next(
            (
                d["name"]
                for d in topo.devices
                if d["role"] == "switch"
                and any(i.get("mode") == "trunk" for i in d.get("interfaces", []))
                and d["name"] in positions
            ),
            None,
        )
        if anchor:
            ax, ay = positions[anchor]
            for i, v in enumerate(vlan_rows):
                notes.append((f"VLAN{v['id']} {v['cidr']}", ax - 40, ay - 34 - i * 24))

    pkt.set_notes(notes)

    pkt.clear_command_logs()
    out_path = Path(out_path)
    pkt.save(out_path)
    return Result(pkt_path=out_path, configs=configs, mapping=mapping, positions=positions)
