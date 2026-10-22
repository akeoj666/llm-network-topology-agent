"""审计护栏：schema 管不了的那部分检查，以及一条可回溯的事件流。

为什么 schema 不够
------------------
JSON Schema 能保证「字段合法」，但保证不了下面这些：

  - 用户需求里夹了「忽略以上所有规则」这类指令，模型有没有被带偏
  - ACL 写了，但第一条就是 permit any any —— 语法完全合法，防护等于零
  - 一台路由器所有接口都 shutdown —— 合法，但这台设备已经废了
  - 三台路由器跑起来谁也不跑路由协议 —— 合法，但跨网段永远不通

这些只能靠语义规则判断，所以说它们是「护栏」而不是「校验」。

两级处理
--------
  block —— 不交付。回灌给模型重写，不通过就不出 pkt。
  warn  —— 记在审计日志里，照常交付，人自己判断。

审计流
-----
`Log` 把每次生成的关键事件按时间顺序记下来：需求原文、模型原始返回、
每轮校验结果、修复动作、护栏裁决、最终产物。
落盘成 JSONL（机器可读）+ Markdown（报告里直接引用）。
一条产物能由此追溯到「哪句话、哪个模型、第几轮、改了什么」。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .dsl import Topology

# 指令性语句的特征词。需求里出现说明有人在「说话」而不是在「描述网络」。
INJECTION_PAT = re.compile(
    r"(忽略|无视|忘掉|不要遵守|不再遵守).{0,10}(之前|以上|上述|前面|所有|系统)?.{0,6}(指令|规则|提示|要求|设定)"
    r"|ignore\s+(all\s+)?(previous|above|prior)"
    r"|disregard\s+(all\s+)?(previous|above)"
    r"|system\s*prompt|jailbreak|越狱提示",
    re.I,
)

# 这些词绝不该出现在设备名/标题里。出现即说明模型照办了注入的指令。
FORBIDDEN_TOKENS = (
    "hacked", "injected", "pwned", "jailbreak", "越狱",
    "忽略", "无视", "ignore", "systemprompt", "系统提示",
)


@dataclass
class Guard:
    code: str
    level: str  # block | warn
    message: str

    def __str__(self) -> str:
        return f"[{'拦截' if self.level == 'block' else '提示'}/{self.code}] {self.message}"


# ---------------------------------------------------------------- 各项护栏
def _check_injection(topo: Topology, prompt: str) -> list[Guard]:
    out: list[Guard] = []
    fields: list[tuple[str, str]] = [
        ("meta.id", str(topo.meta.get("id", ""))),
        ("meta.title", str(topo.meta.get("title", ""))),
        ("meta.source_prompt", str(topo.meta.get("source_prompt", ""))[:200]),
        ("meta.notes", str(topo.meta.get("notes", ""))[:200]),
    ]
    for d in topo.devices:
        fields.append((f"{d.get('name')}.name", str(d.get("name"))))

    for where, text in fields:
        low = text.lower().replace(" ", "")
        hit = next((t for t in FORBIDDEN_TOKENS if t in low), None)
        if hit:
            out.append(Guard("INJECT_OUTPUT", "block", f"{where} 里出现了「{hit}」，疑似照办了需求里的指令"))

    if prompt and INJECTION_PAT.search(prompt):
        out.append(
            Guard("INJECT_PROMPT", "warn", "需求里含指令性语句，已按普通文本处理（不改变系统规则）")
        )
    return out


def _check_dangerous_acl(topo: Topology) -> list[Guard]:
    out: list[Guard] = []
    for acl in topo.data.get("acls", []):
        rules = [r for r in acl.get("rules", []) if isinstance(r, dict)]
        if not rules:
            continue
        first = rules[0]
        if (
            first.get("action") == "permit"
            and str(first.get("source", "any")) == "any"
            and str(first.get("destination", "any")) == "any"
            and first.get("protocol", "ip") in ("ip", None)
        ):
            out.append(
                Guard("ACL_PERMIT_ALL_FIRST", "block",
                      f"ACL {acl.get('id')} 第一条就是 permit ip any any，后面的规则永远不会命中，等于没做防护")
            )
        if rules[-1].get("action") != "permit":
            out.append(
                Guard("ACL_NO_FALLBACK", "warn",
                      f"ACL {acl.get('id')} 最后一条不是 permit，未列出的流量会被隐含 deny 掉")
            )
        if not acl.get("apply"):
            out.append(
                Guard("ACL_UNUSED", "warn", f"ACL {acl.get('id')} 定义了但没挂到任何接口，不会生效")
            )
    return out


def _check_dead_device(topo: Topology) -> list[Guard]:
    out: list[Guard] = []
    for d in topo.devices:
        if d["role"] not in ("router", "switch"):
            continue
        ifaces = [i for i in d.get("interfaces", []) if isinstance(i, dict)]
        if not ifaces:
            out.append(Guard("NO_INTERFACES", "warn", f"{d['name']} 没有声明任何接口"))
            continue
        if all(i.get("shutdown") for i in ifaces):
            out.append(
                Guard("ALL_SHUTDOWN", "block", f"{d['name']} 的 {len(ifaces)} 个接口全部 shutdown，这台设备是死的")
            )
    return out


def _check_routing(topo: Topology) -> list[Guard]:
    routers = [d for d in topo.devices if d["role"] == "router"]
    if len(routers) < 2:
        return []
    protos = {}
    for r in topo.data.get("routing", []):
        if isinstance(r, dict):
            protos[r.get("device")] = r.get("protocol")
    idle = [r["name"] for r in routers if protos.get(r["name"], "none") in (None, "none")]
    if len(idle) == len(routers):
        return [
            Guard("NO_ROUTING", "warn",
                  f"{len(routers)} 台路由器都没跑路由协议，跨网段不会通（需求里如果没要求路由，可忽略）")
        ]
    if idle:
        return [Guard("PARTIAL_ROUTING", "warn", f"这些路由器没跑路由协议：{', '.join(idle)}")]
    return []


def _check_naming(topo: Topology) -> list[Guard]:
    out: list[Guard] = []
    prefix = {"router": ("R", "RT"), "switch": ("SW", "S"), "pc": ("PC",), "server": ("SRV", "SERVER", "S")}
    for d in topo.devices:
        name = str(d.get("name", ""))
        allowed = prefix.get(d["role"], ())
        if allowed and not any(name.upper().startswith(p) for p in allowed):
            out.append(
                Guard("NAME_STYLE", "warn", f"{name} 是 {d['role']}，建议用 {'/'.join(allowed)} 开头，方便读图和写报告")
            )
    return out


def _check_echo(topo: Topology, prompt: str) -> list[Guard]:
    """标题直接把需求原文抄过来——说明模型偷懒没做概括。"""
    title = str(topo.meta.get("title", "")).strip()
    src = (prompt or "").strip()
    if len(src) > 12 and title and src.startswith(title) and len(title) > 12:
        return [Guard("TITLE_ECHO", "warn", "标题是需求原文的开头，建议概括成短标题")]
    return []


def guard(topo: Topology, prompt: str = "") -> list[Guard]:
    out: list[Guard] = []
    out += _check_injection(topo, prompt)
    out += _check_dangerous_acl(topo)
    out += _check_dead_device(topo)
    out += _check_routing(topo)
    out += _check_echo(topo, prompt)
    out += _check_naming(topo)
    return out


def blocked(guards: list[Guard]) -> list[Guard]:
    return [g for g in guards if g.level == "block"]


# ---------------------------------------------------------------- 事件流
@dataclass
class Event:
    kind: str
    data: dict
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"t": round(self.ts, 3), "kind": self.kind, **_clean(self.data)}


def _clean(obj):
    """保证能 JSON 序列化，同时别把超长文本原样塞进去。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, str):
        return obj if len(obj) <= 4000 else obj[:4000] + "…(截断)"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return str(obj)


@dataclass
class Log:
    """一次生成过程的审计日志。"""

    prompt: str = ""
    model: str = ""
    events: list[Event] = field(default_factory=list)

    def add(self, kind: str, **data) -> None:
        self.events.append(Event(kind, data))

    # ---------- 落盘 ----------
    def to_jsonl(self) -> str:
        rows = [json.dumps({"kind": "meta", "prompt": self.prompt, "model": self.model},
                           ensure_ascii=False)] if (self.prompt or self.model) else []
        rows += [json.dumps(e.to_dict(), ensure_ascii=False) for e in self.events]
        return "\n".join(rows) + "\n"

    def write_jsonl(self, path: Path) -> None:
        path.write_text(self.to_jsonl(), encoding="utf-8")

    def to_markdown(self, extra: dict | None = None) -> str:
        lines = ["# 生成审计日志", ""]
        if self.prompt:
            lines += [f"- 需求原文：{self.prompt}", ""]
        if self.model:
            lines += [f"- 模型：{self.model}"]
        for k, v in (extra or {}).items():
            lines.append(f"- {k}：{v}")
        lines.append("")

        rounds = [e for e in self.events if e.kind == "round"]
        if rounds:
            lines += ["## 每轮结果", "", "| 轮次 | 校验错误 | 护栏拦截 | 耗时 |", "|---|---|---|---|"]
            for e in rounds:
                d = e.data
                lines.append(
                    f"| {d.get('round')} | {d.get('errors', 0)} | {d.get('blocked', 0)} | "
                    f"{d.get('elapsed', 0):.1f}s |"
                )
            lines.append("")

        blocks = [e for e in self.events if e.kind == "guard"]
        if blocks:
            lines += ["## 护栏裁决", ""]
            for e in blocks:
                for g in e.data.get("items", []):
                    lines.append(f"- {g}")
            lines.append("")

        screen = [e for e in self.events if e.kind == "cmdscreen"]
        if screen:
            s = screen[-1].data
            lines += [
                "## 命令级筛查",
                "",
                "| 指标 | 数值 |",
                "|---|---|",
                f"| 配置行 | {s.get('total')} 行，{len(s.get('devices') or {})} 台设备 |",
                f"| 拦截 / 告警 | {s.get('blocked')} / {s.get('warnings')} |",
                "",
            ]
            items = s.get("items") or []
            if items:
                lines += ["命中明细：", ""]
                lines += [f"- {x}" for x in items]
                lines.append("")
            else:
                lines += ["> 全部命令都在白名单内，未命中黑名单。", ""]

        confirm = [e for e in self.events if e.kind == "confirm"]
        if confirm:
            c = confirm[-1].data
            lines += [
                "## 部署确认",
                "",
                f"- 确认来源：**{c.get('source')}**",
                f"- 确认对象：{c.get('devices')} 台设备 / {c.get('lines')} 行配置",
                "",
            ]

        fixes = [e for e in self.events if e.kind == "repair"]
        if fixes:
            lines += ["## 自动修复", ""]
            for e in fixes:
                lines.append(f"### 第 {e.data.get('round')} 轮")
                lines.append("")
                for f in e.data.get("fixed", []):
                    lines.append(f"- {f}")
                lines.append("")

        errs = [e for e in self.events if e.kind == "validate" and e.data.get("issues")]
        for e in errs:
            lines += [f"## 第 {e.data.get('round')} 轮校验问题", ""]
            lines += [f"- {i}" for i in e.data["issues"]]
            lines.append("")

        deliver = [e for e in self.events if e.kind == "deliver"]
        if deliver:
            d = deliver[-1].data
            devs: dict = d.get("devices") or {}
            total = sum(len(v) for v in devs.values() if isinstance(v, list))
            lines += ["## 下发内容", ""]
            lines.append(f"- 产物：`{d.get('pkt')}`")
            lines.append(f"- 设备 {len(devs)} 台，合计 {total} 行配置")
            lines.append("")
            lines += ["| 设备 | 配置行数 |", "|---|---|"]
            for name, body in devs.items():
                n = len(body) if isinstance(body, list) else 0
                lines.append(f"| {name} | {n if n else '—（主机，无 CLI 配置）'} |")
            lines.append("")
            lines.append("<details><summary>展开全部下发配置</summary>")
            lines.append("")
            for name, body in devs.items():
                if not isinstance(body, list):
                    continue
                if not body:
                    lines += [f"**{name}**", "", "（主机，地址在图形界面里配，无 CLI 配置）", ""]
                    continue
                lines += [f"**{name}**", "", "```", *[str(x) for x in body], "```", ""]
            lines += ["</details>", ""]

        verify = [e for e in self.events if e.kind == "verify"]
        if verify:
            v = verify[-1].data
            lines += ["## 验证结果", ""]
            lines += [
                "| 指标 | 数值 |",
                "|---|---|",
                f"| 推算主机对 | {v.get('pairs')} 组（通 {v.get('reach')} / 不通 {v.get('block')}） |",
                f"| 断言 | {v.get('asserts_total')} 条，未通过 {v.get('asserts_failed')} 条 |",
                f"| 一致性检查 | {v.get('checks_total')} 项，未通过 {v.get('checks_failed')} 项 |",
                "",
            ]
            if v.get("asserts_failed") or v.get("checks_failed"):
                lines += ["未通过明细：", ""]
                lines += [f"- {x}" for x in (v.get("details") or [])]
                lines.append("")
            else:
                lines += [
                    "> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。",
                    "> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。",
                    "",
                ]

        return "\n".join(lines)
