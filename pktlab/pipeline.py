"""一条龙：Topology → .pkt + 拓扑图 + 配置清单。

gen.py（从 YAML 生成）和 nlgen.py（从一句话生成）都走这里，避免两套逻辑漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import accept, assemble, cmdguard, config
from .diagram import render
from .dsl import Topology


@dataclass
class Products:
    pkt: Path
    png: Path
    svg: Path
    configs: Path
    configs_by_dev: dict[str, list[str]] = field(default_factory=dict)


PASTE_GUIDE = """\
## 怎么用（先说结论）

**双击 `{uid}.pkt` 打开，配置已经在里面了，通常不需要手工粘贴。**
打开后等 30 秒（STP 收敛 + ARP 学习），就可以按验收报告里挑的用例去 ping。

下面这份配置清单有三个用途：

1. 报告里要附的「分设备配置」
2. 想在**别的拓扑/别的 pkt** 里复现某台设备的配置
3. 万一 pkt 打不开或配置丢了，照这个手工补

---

## 方式一：直接打开 pkt（推荐）

1. 双击 `{uid}.pkt`，Packet Tracer 自动加载
2. 等 **30 秒**（生成树收敛，过早 ping 会丢包）
3. 看连线是否变绿；绿色三角＝链路 up
4. 按 `{uid}_验收报告.md` 里的「预期不通」抽 2 条、「预期通」抽 2 条，在 PC 的
   `Desktop → Command Prompt` 里 ping 验证

## 方式二：命令行整段粘贴（复现/兜底用）

以某台路由器为例：

1. 点开设备 → 切到 **CLI** 标签页
2. 按**回车**唤出提示符 `Router>`，敲 `enable` 进特权模式（`Router#`）
3. 敲 `configure terminal`（`Router(config)#`）
4. 把本文件里该设备的代码块**整段复制**，在 CLI 窗口里**右键 → Paste**
5. 敲 `end` 回到特权模式，再敲 `write memory` 存盘
6. 用 `show running-config` 跟本文件比对

**三个必须注意的点：**

- **PT 是逐行回放，遇到第一行非法命令就中止后面全部**。
  所以粘贴后一定要看一眼有没有 `% Invalid input` —— 出现就说明后面的都没生效。
- 粘贴前确认设备**型号一致**（`Router-PT-Empty` / `2960-24TT` / `PC-PT`）。
  比如 `Router-PT-Empty` 没有 `FastEthernet0/0`，粘 2811 的配置必然第一行就废。
- 粘贴是**追加**不是覆盖。设备里已有旧配置时，先 `erase startup-config` +
  `reload`，或者手工 `no` 掉冲突项。

## 方式三：主机 IP 不是用 CLI 配的

PC / Server 的地址要在图形界面里填，配置清单里的「主机」小节只是给你对照用的数字：

1. 点开 PC → **Desktop** 标签 → **IP Configuration**
2. 选 **Static**
3. 填 IPv4 Address / Subnet Mask / Default Gateway（清单里逐台列好了）
4. 关掉窗口即生效

> 注意：PT 里网关存两份（端口级和引擎级），生成器两处都写了。
> 如果你手工只改了一处，会出现「命令行里网关是对的、但 IP Configuration 窗口是空的」。

## 方式四：VLAN 数据库

交换机上的 `vlan 10` / `name RD` 这类命令**不能只靠命令行粘贴**——
PT 的 VLAN 数据库不在 `running-config` 里，而在设备内部（`flash:vlan.dat`）。
生成器是直接写进 pkt 容器的，所以打开就有。

如果你在**别的交换机**上手工复现，粘贴 `running-config` 后还要单独执行：

```
vlan 10
 name RD
vlan 20
 name FIN
exit
```

然后用 `show vlan brief` 确认 VLAN 真的出现了（端口归属也要对）。

---
"""


def write_configs(path: Path, topo: Topology, configs: dict[str, list[str]]) -> None:
    uid = topo.meta.get("id", "topology")
    lines = [f"# {uid} {topo.meta.get('title', '')}", ""]
    if topo.meta.get("source_prompt"):
        lines += [f"> 需求原文：{topo.meta['source_prompt']}", ""]

    lines += [PASTE_GUIDE.format(uid=uid)]

    lines.append("## 接线表")
    lines.append("")
    lines.append("| 设备 | 端口 | 对端设备 | 对端端口 | 线型 |")
    lines.append("|---|---|---|---|---|")
    for ln in topo.links:
        a_dev, _, a_port = ln["from"].partition(":")
        b_dev, _, b_port = ln["to"].partition(":")
        cable = {"serial": "串口线", "cross": "交叉线", "copper": "直通线"}.get(
            assemble.effective_cable(topo, ln), "直通线"
        )
        lines.append(f"| {a_dev} | {a_port} | {b_dev} | {b_port} | {cable} |")
    lines.append("")

    vlans = topo.data.get("vlans", [])
    if vlans:
        lines += ["## VLAN 规划", "", "| VLAN | 名称 | 网段 |", "|---|---|---|"]
        for v in vlans:
            lines.append(f"| {v['id']} | {v.get('name', '')} | {v.get('cidr', '')} |")
        lines.append("")

    for d in topo.devices:
        cfg = configs.get(d["name"]) or []
        if not cfg:
            if d.get("ip"):
                lines += [f"## {d['name']}（主机）", "", f"- IP：{d['ip']}", f"- 网关：{d.get('gateway', '')}", ""]
            continue
        lines += [f"## {d['name']}（{d['role']}）", "", "```", *cfg, "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run(
    topo: Topology,
    out_dir: str | Path,
    *,
    confirmer=None,
    log=None,
) -> Products:
    """一条龙生成。**命令级筛查和确认门都在这里，没通过不会写出 .pkt。**

    顺序：生成配置行 → 命令级筛查（白名单/黑名单）→ 人工 CONFIRM → 装配落盘。
    任何一步不通过都抛异常，磁盘上不会出现新的 pkt —— 这是护栏能叫护栏的前提。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    uid = topo.meta.get("id", "topology")

    # ① 先把配置行算出来，才能筛；此时还没碰 pkt
    configs = config.generate_all(topo)
    gate = cmdguard.check_all(configs)
    if log is not None:
        log.add(
            "cmdscreen",
            total=gate.total_lines,
            devices=gate.devices,
            blocked=len(gate.blocked),
            warnings=len(gate.warnings),
            items=[str(i) for i in gate.issues],
        )
    if not gate.passed:
        raise cmdguard.CmdBlocked(gate)

    # ② 部署前人工确认门（默认交互；没有 --yes 之类的旁路）
    confirmer = confirmer or cmdguard.interactive_confirm
    source = confirmer(gate, configs)
    if log is not None:
        log.add("confirm", source=source, devices=len(configs), lines=gate.total_lines)

    result = assemble.build(topo, out / f"{uid}.pkt")
    png = out / f"{uid}_topology.png"
    svg = out / f"{uid}_topology.svg"
    render(topo, png, svg)
    cfg_path = out / f"{uid}_configs.md"
    write_configs(cfg_path, topo, result.configs)
    return Products(out / f"{uid}.pkt", png, svg, cfg_path, result.configs)


def finalize_audit(
    log,
    topo: Topology,
    products: Products,
    out_dir: str | Path,
    summary: dict | None = None,
) -> Path | None:
    """把「下发内容」和「验证结果」补进审计日志，并落盘。

    评分标准 E④ 要求审计日志覆盖 prompt、原始输出、拦截记录、**下发内容**、**验证结果**。
    前三个在生成过程中就有了；后两个必须等 pkt 真正造出来、验收跑完才有，
    所以单独放在最后一步。
    """
    if log is None:
        return None
    out = Path(out_dir)
    uid = topo.meta.get("id", "topology")

    # 下发内容：每台设备真正写进 pkt 的配置行
    log.add(
        "deliver",
        pkt=products.pkt.name,
        devices={name: lines for name, lines in products.configs_by_dev.items()},
    )

    # 验证结果：验收器的结论（这一步是静态推算，报告里已声明口径）
    rep = accept.analyze(topo)
    log.add(
        "verify",
        pairs=len(rep.pairs),
        reach=rep.n_reach,
        block=rep.n_block,
        asserts_total=len(rep.asserts),
        asserts_failed=len(rep.failed_asserts),
        checks_total=len(rep.checks),
        checks_failed=len(rep.failed_checks),
        details=[str(a) for a in rep.failed_asserts],
    )

    jl = out / f"{uid}_audit.jsonl"
    log.write_jsonl(jl)
    (out / f"{uid}_nlog.md").write_text(log.to_markdown(summary), encoding="utf-8")
    return jl

