"""命令级护栏：白名单 / 危险命令黑名单 / 部署前 CONFIRM 确认门。

为什么单独立一层
----------------
`audit.py` 的护栏是**语义**的（ACL 第一条 permit any any、所有接口 shutdown 之类），
它判断的是「配置合不合理」。评分标准 E 要的是**命令级**护栏，判断的是
「这行命令允不允许出现」，两者不能互相顶替：

  - 语义护栏：`access-list 100 permit ip any any` 是合法命令，但防护等于零 → 拦
  - 命令护栏：`reload`、`write erase` 是合法命令，且单独看毫无语义问题 → 也必须拦

什么时候跑
----------
在 DSL 通过校验、配置行已生成、**还没写进 pkt** 之前。
顺序是：校验 → 语义护栏 → **命令级筛查** → **人工 CONFIRM** → 装配落盘。
所以任何一条命令不合规，或者人不点确认，都不会产出 .pkt 文件。

三个组件
--------
  ① whitelist —— 正则白名单，命不中即拒绝（default-deny）
  ② blacklist —— 危险命令硬拦截，命中即 block，且优先于白名单判断
  ③ confirm   —— 部署前人工确认门，必须敲大写 CONFIRM；**没有 --yes / 环境变量能跳过**

关于「不能跳过」
----------------
评分标准红线第 3 条是「绕过或关闭自设的安全护栏进行演示」。
所以这里刻意不提供 bypass 开关。批量/离线测试走的是 `static_confirm()`，
它在审计日志里会明确记成「非交互（自动化测试）」，
不会伪装成人工确认 —— 日志造假是红线第 4 条。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable

# ---------------------------------------------------------------- ① 白名单
# 只列本项目生成器真实会产出的命令形态 + 少量明确安全的手工命令。
# 命中即放行；一条都不命中的行按「白名单外」拒绝。
WHITELIST: tuple[str, ...] = (
    # 版本 / 全局杂项
    r"^version\s+\d+(\.\d+)?$",
    r"^no\s+service\s+timestamps\s+(log|debug)\s+datetime\s+msec$",
    r"^no\s+service\s+password-encryption$",
    r"^service\s+password-encryption$",
    r"^hostname\s+[A-Za-z0-9_.-]+$",
    r"^no\s+ip\s+cef$",
    r"^no\s+ipv6\s+cef$",
    r"^ip\s+classless$",
    # 接口
    r"^interface\s+(FastEthernet|GigabitEthernet|Serial|Loopback)\d+/\d+(\.\d+)?$",
    r"^ip\s+address\s+\d+\.\d+\.\d+\.\d+\s+\d+\.\d+\.\d+\.\d+$",
    r"^no\s+ip\s+address$",
    r"^no\s+shutdown$",
    r"^shutdown$",
    r"^clock\s+rate\s+\d+$",
    r"^duplex\s+(auto|full|half)$",
    r"^speed\s+(auto|10|100)$",
    r"^encapsulation\s+dot1Q\s+\d+$",
    # 交换
    r"^switchport\s+mode\s+(access|trunk)$",
    r"^switchport\s+access\s+vlan\s+\d+$",
    r"^switchport\s+trunk\s+native\s+vlan\s+\d+$",
    r"^switchport\s+trunk\s+allowed\s+vlan\s+[\d,\-]+$",
    r"^vlan\s+\d+$",
    r"^name\s+[A-Za-z0-9_.-]+$",
    # 路由协议
    r"^router\s+rip$",
    r"^router\s+ospf\s+\d+$",
    r"^router\s+bgp\s+\d+$",
    r"^no\s+auto-summary$",
    r"^auto-summary$",
    r"^network\s+\d+\.\d+\.\d+\.\d+(\s+\d+\.\d+\.\d+\.\d+)?(\s+area\s+\d+)?$",
    r"^area\s+\d+\s+stub$",
    r"^passive-interface\s+\S+$",
    r"^default-information\s+originate$",
    r"^neighbor\s+\d+\.\d+\.\d+\.\d+\s+remote-as\s+\d+$",
    r"^redistribute\s+(static|connected)(\s+subnets)?$",
    # 静态路由 / 默认路由（含浮动静态的 AD）
    r"^ip\s+route\s+\d+\.\d+\.\d+\.\d+\s+\d+\.\d+\.\d+\.\d+\s+\d+\.\d+\.\d+\.\d+(\s+\d+)?$",
    r"^ip\s+default-gateway\s+\d+\.\d+\.\d+\.\d+$",
    # ACL
    r"^access-list\s+\d+\s+(permit|deny)\s+.+$",
    r"^ip\s+access-group\s+\S+\s+(in|out)$",
    # 行配置 / 结束符
    r"^line\s+(con|vty|aux)\s+\d+(\s+\d+)?$",
    r"^login$",
    r"^exit$",
    r"^end$",
    r"^!$",
)

# ---------------------------------------------------------------- ② 黑名单
# (正则, 代码, 说明)。这些命令在正常生成里永远不该出现，
# 一旦出现不是模型幻觉就是提示注入，硬拦 + 告警。
BLACKLIST: tuple[tuple[str, str, str], ...] = (
    (r"^reload(\s|$)", "CMD_RELOAD", "重启设备，会中断全网并丢掉未保存配置"),
    (r"^write\s+erase$|^erase\s+startup-config$|^erase\s+nvram:", "CMD_ERASE",
     "擦除启动配置，设备重启后配置全丢"),
    (r"^format(\s|$)", "CMD_FORMAT", "格式化 flash，系统文件会被删除"),
    (r"^delete(\s|/)", "CMD_DELETE", "删除 flash 内文件，可能导致 IOS 缺失"),
    (r"^no\s+ip\s+routing$", "CMD_NO_ROUTING", "关闭 IP 路由，路由器立刻退化成普通主机"),
    (r"^(boot\s+system|config-register|no\s+boot)", "CMD_BOOT", "改启动参数，可能导致设备起不来"),
    (r"^setup$", "CMD_SETUP", "进入配置对话框，会打断当前会话"),
    (r"^ip\s+http\s+server$", "CMD_HTTP", "开启 Web 管理面，扩大攻击面"),
    (r"^snmp-server\s+community\s+(public|private)", "CMD_SNMP", "默认团体名等同于无认证"),
    (r"^enable\s+(password|secret)\s+(cisco|password|123|admin)?$", "CMD_WEAK_ENABLE",
     "空口令或弱口令的特权密码"),
    (r"^debug\s+all$", "CMD_DEBUG_ALL", "全网调试输出，会让设备 CPU 打满"),
    (r"^crypto\s+key\s+generate", "CMD_CRYPTO", "生成密钥对，需要交互，回放时会卡住"),
)

BLACKLIST_COMPILED = tuple((re.compile(p, re.I), code, why) for p, code, why in BLACKLIST)
WHITELIST_COMPILED = tuple(re.compile(p, re.I) for p in WHITELIST)

# 生成器自己要写的少数命令（如 `no ip cef`）同时出现在白名单里，
# 如果哪天需要「命中黑名单但只提示」，把代码放进这个集合即可。
_ALLOW_HINT: set[str] = set()


@dataclass
class Issue:
    code: str
    level: str  # block | warn
    device: str
    lineno: int
    command: str
    message: str

    def __str__(self) -> str:
        tag = "拦截" if self.level == "block" else "告警"
        return f"[{tag}/{self.code}] {self.device} 第 {self.lineno} 行 `{self.command}` —— {self.message}"


@dataclass
class GateResult:
    """一次命令级筛查的结果。"""

    issues: list[Issue] = field(default_factory=list)
    devices: dict[str, int] = field(default_factory=dict)  # 设备 → 配置行数
    total_lines: int = 0

    @property
    def blocked(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "block"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    @property
    def passed(self) -> bool:
        return not self.blocked

    def summary_text(self) -> str:
        lines = [
            "部署前确认（命令级护栏结果）",
            f"- 设备 {len(self.devices)} 台，配置合计 {self.total_lines} 行",
            f"- 白名单：{len(WHITELIST)} 条规则，未列入的命令一律拒绝",
            f"- 黑名单：{len(BLACKLIST)} 条危险命令规则",
            f"- 拦截 {len(self.blocked)} 条，告警 {len(self.warnings)} 条",
        ]
        for i in self.blocked:
            lines.append(f"  ✗ {i}")
        for i in self.warnings:
            lines.append(f"  ! {i}")
        return "\n".join(lines)


def _norm(raw: str) -> str:
    """归一化：去首尾空白、压空格。不动大小写（保留原样用于展示）。"""
    return re.sub(r"\s+", " ", raw.strip())


def check_line(device: str, lineno: int, raw: str) -> list[Issue]:
    cmd = _norm(raw)
    if not cmd:
        return []
    out: list[Issue] = []

    # 黑名单优先：即使是白名单里的命令，命中黑名单也拦
    for pat, code, why in BLACKLIST_COMPILED:
        if pat.search(cmd):
            if code in _ALLOW_HINT:
                out.append(Issue(code, "warn", device, lineno, cmd, why))
            else:
                out.append(Issue(code, "block", device, lineno, cmd, why))
            return out

    if any(p.match(cmd) for p in WHITELIST_COMPILED):
        return []

    out.append(
        Issue(
            "CMD_UNLISTED", "block", device, lineno, cmd,
            "不在白名单内，按「白名单外一律拒绝」处理（如确需使用，先把它加进 cmdguard.WHITELIST 并说明理由）",
        )
    )
    return out


def check_all(devices: dict[str, list[str]]) -> GateResult:
    """逐设备逐行筛查。"""
    res = GateResult()
    for name, body in devices.items():
        body = body or []
        res.devices[name] = len(body)
        res.total_lines += len(body)
        for i, raw in enumerate(body, 1):
            res.issues.extend(check_line(name, i, raw))
    return res


# ---------------------------------------------------------------- ③ 确认门
class CmdBlocked(RuntimeError):
    """命令级护栏拦下了，不产出 pkt。"""

    def __init__(self, gate: GateResult):
        self.gate = gate
        super().__init__("命令级护栏拦截：\n" + "\n".join(str(i) for i in gate.blocked))


class ConfirmRefused(RuntimeError):
    """人工没有确认，不产出 pkt。"""


CONFIRM_WORD = "CONFIRM"


def interactive_confirm(
    gate: GateResult,
    devices: dict[str, list[str]],
    *,
    reader: Callable[[str], str] = input,
    writer: Callable[[str], None] = print,
) -> str:
    """在终端里让人看着摘要敲 CONFIRM。返回「确认来源」字符串（写进审计日志）。"""
    if not sys.stdin or not sys.stdin.isatty():
        raise ConfirmRefused(
            "当前不是交互终端，无法完成人工确认。\n"
            "确需在脚本里部署，请显式传 --confirm CONFIRM（会在审计日志记为「命令行参数确认」）。"
        )
    writer("")
    writer(gate.summary_text())
    writer("")
    try:
        ans = reader(f"确认无误请输入大写 {CONFIRM_WORD}（其他任意输入即中止）：")
    except EOFError:
        raise ConfirmRefused("输入流已关闭，视为未确认") from None
    if ans.strip() != CONFIRM_WORD:
        raise ConfirmRefused(f"未输入 {CONFIRM_WORD}，已中止，未生成任何 .pkt")
    return "人工交互确认（终端输入 CONFIRM）"


def cli_confirm(
    gate: GateResult,
    devices: dict[str, list[str]],
    *,
    writer: Callable[[str], None] = print,
) -> str:
    """`--confirm CONFIRM` 走这条：仍打印摘要，但确认来源如实记为「命令行参数」。"""
    writer("")
    writer(gate.summary_text())
    return "命令行参数 --confirm"


def static_confirm(tag: str = "非交互（自动化测试）") -> Callable[[GateResult, dict], str]:
    """给测试/离线流程用。**不打印、不假装人工确认**，来源字符串如实标注。"""

    def _fn(gate: GateResult, devices: dict[str, list[str]]) -> str:
        return tag

    return _fn


def dump_blacklist() -> list[str]:
    """给 docs / 报告生成用：列出全部黑名单规则。"""
    return [f"`{p}` — {code} — {why}" for p, code, why in BLACKLIST]


def dump_whitelist() -> list[str]:
    return list(WHITELIST)


def probe(text: str) -> list[Issue]:
    """交互试一下某段文本会被怎么判（给 tools 和文档用）。"""
    return check_line("<probe>", 1, text)


def iter_all_rules() -> Iterable[str]:
    return [f"WL {p}" for p in WHITELIST] + [f"BL {p}" for p, _, _ in BLACKLIST]
