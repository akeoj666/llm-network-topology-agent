"""命令级护栏演示（给报告/答辩用）：三个场景，真实跑，输出直接可截图。

  场景 1  正常配置 → 0 拦截，给出确认摘要
  场景 2  配置里混进 `reload` / `write erase` / `no ip routing` → 硬拦截，不产出 pkt
  场景 3  白名单外的命令（模型幻觉，如 `ip domain-name`）→ 拒绝

    python tools/demo_cmdguard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import cmdguard, config, pipeline  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

LINE = "-" * 72


def _load():
    topo = Topology.load(ROOT / "examples" / "uc01.yaml")
    topo.validate()
    return topo


def scenario_normal() -> None:
    print(LINE)
    print("场景 1：正常配置（UC-01，本地模板生成）")
    print(LINE)
    topo = _load()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = pipeline.run(topo, tmp, confirmer=cmdguard.static_confirm("演示（static_confirm）"))
        print(f"结果：通过，产出 {p.pkt.name}")
        print("说明：演示里用 static_confirm 跳过交互；真实部署走 pipeline.run() 的默认")
        print("      interactive_confirm，会在装配前停下来要求手敲 CONFIRM。")


def scenario_dangerous() -> None:
    print()
    print(LINE)
    print("场景 2：配置里混进危险命令（模拟模型幻觉 / 提示注入）")
    print(LINE)
    topo = _load()
    real = config.generate_all

    def poisoned(t):
        cfgs = real(t)
        cfgs["R1"].insert(-1, "reload in 5")
        cfgs["R2"].insert(-1, "write erase")
        cfgs["R3"].insert(-1, "no ip routing")
        return cfgs

    config.generate_all = poisoned
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            try:
                pipeline.run(topo, tmp, confirmer=cmdguard.static_confirm("演示"))
                print("结果：居然通过了（这说明护栏失效）")
            except cmdguard.CmdBlocked as exc:
                print("结果：拦截，未生成任何 .pkt")
                for i in exc.gate.blocked:
                    print(f"  {i}")
                print(f"  目录内容：{sorted(p.name for p in tmp.glob('*')) or '（空）'}")
    finally:
        config.generate_all = real


def scenario_unlisted() -> None:
    print()
    print(LINE)
    print("场景 3：白名单外的命令（default-deny）")
    print(LINE)
    for line in (
        "ip domain-name corp.example",
        "username ops privilege 15 secret 5 $1$xyz",
        "ip nat inside source list 1 interface FastEthernet2/0 overload",
        "banner motd #Unauthorized access prohibited#",
    ):
        issues = cmdguard.probe(line)
        first = issues[0] if issues else None
        print(f"  `{line}`")
        print(f"    → {first.code if first else 'PASS'} ({first.level if first else '-'})")


def main() -> int:
    print("命令级护栏演示（离线，不联网，不启动 Packet Tracer）")
    print()
    print(f"白名单 {len(cmdguard.WHITELIST)} 条规则；黑名单 {len(cmdguard.BLACKLIST)} 条规则")
    print()
    print("黑名单清单：")
    for item in cmdguard.dump_blacklist():
        print(f"  - {item}")
    print()
    scenario_normal()
    scenario_dangerous()
    scenario_unlisted()
    print()
    print(LINE)
    print("要点：拦截发生在装配之前，磁盘上不会出现 pkt；确认来源会写进审计日志。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
