"""命令级护栏的离线测试：白名单 / 黑名单 / 确认门 / 不落盘。

验证四件事：
  ① 黑名单里的危险命令一定被 block，且错误码对得上
  ② 白名单外的命令一定被 block（default-deny）
  ③ 正常生成的配置 0 拦截 0 告警（护栏不能把正常流程搞死）
  ④ 拦截或未确认时，磁盘上不会出现新的 .pkt（这是「护栏」和「建议」的分界线）

不需要 API key，也不联网。
    python tools/test_cmdguard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import cmdguard, config, pipeline  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- ① 黑名单
DANGEROUS = [
    ("reload", "CMD_RELOAD"),
    ("reload in 5", "CMD_RELOAD"),
    ("write erase", "CMD_ERASE"),
    ("erase startup-config", "CMD_ERASE"),
    ("format flash:", "CMD_FORMAT"),
    ("delete flash:c2800nm-advipservicesk9-mz.124-15.T1.bin", "CMD_DELETE"),
    ("no ip routing", "CMD_NO_ROUTING"),
    ("boot system flash:c2800.bin", "CMD_BOOT"),
    ("config-register 0x2102", "CMD_BOOT"),
    ("ip http server", "CMD_HTTP"),
    ("snmp-server community public RO", "CMD_SNMP"),
    ("enable password cisco", "CMD_WEAK_ENABLE"),
    ("debug all", "CMD_DEBUG_ALL"),
    ("crypto key generate rsa", "CMD_CRYPTO"),
]

print("① 黑名单：危险命令必须全部拦截")
for line, code in DANGEROUS:
    got = cmdguard.probe(line)
    hit = bool(got) and got[0].code == code and got[0].level == "block"
    check(f"`{line}` → {code}", hit, f"实际 {[str(i) for i in got]}")

# 大小写、前后空格、多余空格都要能识别（模型很爱返回 `Reload` / `  reload  `）
for variant in ("RELOAD", "  reload  ", "write   erase", "No IP Routing"):
    got = cmdguard.probe(variant)
    check(f"归一化后仍能识别 `{variant!r}`", bool(got) and got[0].level == "block", str(got))


# ---------------------------------------------------------------- ② 白名单
print("\n② 白名单：未列入的命令一律拒绝")
UNLISTED = [
    "ip domain-name example.com",
    "username admin privilege 15 secret 5 $1$abc",
    "logging host 10.0.0.5",
    "ip nat inside source list 1 interface Fa2/0 overload",
    "banner motd #hello#",
]
for line in UNLISTED:
    got = cmdguard.probe(line)
    ok = bool(got) and got[0].code == "CMD_UNLISTED" and got[0].level == "block"
    check(f"`{line}` 被拒", ok, f"实际 {[str(i) for i in got]}")

print("\n   白名单内的命令应当放行")
ALLOWED = [
    "version 12.2",
    "hostname R1",
    "no ip cef",
    "interface FastEthernet2/0",
    "interface FastEthernet2/0.10",
    "ip address 192.168.16.62 255.255.255.192",
    "no shutdown",
    "clock rate 2000000",
    "encapsulation dot1Q 10",
    "switchport mode trunk",
    "switchport access vlan 20",
    "vlan 10",
    "router rip",
    "version 2",
    "no auto-summary",
    "network 192.168.16.0",
    "network 10.0.0.0 0.0.0.255 area 0",
    "router ospf 100",
    "area 0 stub",
    "access-list 100 deny ip 192.168.20.0 0.0.0.255 any",
    "ip access-group 100 in",
    "ip route 0.0.0.0 0.0.0.0 10.0.0.1 10",
    "ip classless",
    "line vty 0 4",
    "login",
    "end",
    "!",
]
for line in ALLOWED:
    got = cmdguard.probe(line)
    check(f"`{line}` 放行", not got, f"实际 {[str(i) for i in got]}")


# ---------------------------------------------------------------- ③ 正常配置
print("\n③ 正常配置：全部内置用例 0 拦截 0 告警")
n_cases = 0
for f in sorted((ROOT / "examples").glob("*.yaml")):
    if "bad" in f.stem:
        continue
    topo = Topology.load(f)
    topo.validate()
    gate = cmdguard.check_all(config.generate_all(topo))
    n_cases += 1
    check(
        f"{f.stem} {gate.total_lines} 行",
        gate.passed and not gate.warnings,
        f"拦截 {[str(i) for i in gate.blocked]}；告警 {[str(i) for i in gate.warnings]}",
    )
check(f"共检查 {n_cases} 个用例", n_cases >= 5)


# ---------------------------------------------------------------- ④ 不落盘
print("\n④ 拦截 / 未确认时不得产出 pkt")
topo = Topology.load(ROOT / "examples" / "uc01.yaml")
topo.validate()
real_gen = config.generate_all


def poisoned(_topo):
    """模拟「模型偷偷加了一条 reload」：DSL 校验完全发现不了。"""
    cfgs = real_gen(_topo)
    cfgs["R1"].insert(-1, "reload")
    return cfgs


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    config.generate_all = poisoned
    try:
        try:
            pipeline.run(topo, tmp, confirmer=cmdguard.static_confirm())
            check("危险命令应抛 CmdBlocked", False, "居然跑过去了")
        except cmdguard.CmdBlocked as exc:
            blocked = [i for i in exc.gate.blocked if i.code == "CMD_RELOAD"]
            check("抛 CmdBlocked 且定位到 reload", bool(blocked), str(exc))
        check("目录里没有生成 pkt", not list(tmp.glob("*.pkt")), str(list(tmp.glob("*"))))
    finally:
        config.generate_all = real_gen

    # 正常配置 + 拒绝确认 → 同样不能落盘
    def refuse(gate, cfgs):
        raise cmdguard.ConfirmRefused("测试：人没敲 CONFIRM")

    try:
        pipeline.run(topo, tmp, confirmer=refuse)
        check("未确认应抛 ConfirmRefused", False, "居然跑过去了")
    except cmdguard.ConfirmRefused:
        check("抛 ConfirmRefused", True)
    check("未确认时也没有 pkt", not list(tmp.glob("*.pkt")), str(list(tmp.glob("*"))))

    # 正常确认 → 应当产出
    p = pipeline.run(topo, tmp, confirmer=cmdguard.static_confirm("测试确认"))
    check("确认后才产出 pkt", p.pkt.is_file())
    check("配置清单里带粘贴指南", "怎么用（先说结论）" in p.configs.read_text(encoding="utf-8"))


print("\n⑤ 确认来源必须如实记录（不能伪装成人工确认）")
from pktlab import audit  # noqa: E402

log = audit.Log()
with tempfile.TemporaryDirectory() as td:
    pipeline.run(topo, Path(td), confirmer=cmdguard.static_confirm("非交互（自动化测试）"), log=log)
kinds = [e.kind for e in log.events]
check("审计里有 cmdscreen 事件", "cmdscreen" in kinds)
check("审计里有 confirm 事件", "confirm" in kinds)
src = next((e.data.get("source") for e in log.events if e.kind == "confirm"), "")
check("确认来源写明是自动化", "自动化" in str(src), str(src))
md = log.to_markdown()
check("Markdown 有命令级筛查小节", "## 命令级筛查" in md)
check("Markdown 有部署确认小节", "## 部署确认" in md)

print()
if FAILS:
    print(f"{len(FAILS)} 项未通过：")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("全部断言通过。")
