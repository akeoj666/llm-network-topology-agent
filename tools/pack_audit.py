"""打包审计日志：把散在 out/ 与 examples/ 里的产物汇到 `out/审计日志/`，
并生成一份能顺着查下去的索引。

为什么需要这个
--------------
评分要求交「审计日志包」。现在日志是散的：`{uid}_audit.jsonl`、`{uid}_nlog.md`、
`{uid}_验收报告.md`、`{uid}_configs.md`、`{uid}.pkt` 各在各处，审查的人不知道
哪个文件属于哪个用例、更不知道「一条结论能追到哪几行配置」。

这个脚本只做三件事：**汇总、索引、如实标注缺口**。
不补写日志、不把「没留档」写成「通过」——早期运行的用例缺什么就写缺什么。

    python tools/pack_audit.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab.dsl import Topology  # noqa: E402

OUT = ROOT / "out"
EXAMPLES = ROOT / "examples"
DEST = OUT / "审计日志"

# 一条「完整」的审计链应该有的五类事件（YAML 通道与 NL 通道都落这五类）
REQUIRED_EVENTS = ("meta", "cmdscreen", "confirm", "deliver", "verify")
# NL 通道额外有：prompt / raw / validate / guard / round / done
NL_EVENTS = ("prompt", "raw", "validate", "round", "done")


def usecases() -> list[dict]:
    """收集所有用例（examples 优先，out 补充），返回元信息。"""
    seen: dict[str, Path] = {}
    for folder in (EXAMPLES, OUT):
        for p in sorted(folder.glob("*.yaml")):
            seen.setdefault(p.stem.replace("-", "").lower(), p)

    out: list[dict] = []
    for key in sorted(seen):
        path = seen[key]
        try:
            topo = Topology.load(path)
        except Exception as exc:  # 坏用例也要列出来，不能悄悄跳过
            out.append({"uid": path.stem, "title": "（加载失败）", "yaml": path,
                        "bad": True, "err": str(exc), "prompt": ""})
            continue
        out.append({
            "uid": topo.meta.get("id", path.stem),
            "title": topo.meta.get("title", ""),
            "yaml": path,
            "bad": "bad" in path.stem.lower(),
            "prompt": str(topo.meta.get("source_prompt") or "").strip(),
            "devices": len(topo.devices),
            "links": len(topo.links),
        })
    return out


def read_events(uid: str) -> tuple[list[str], int]:
    """返回 (事件类型序列, 有效行数)。坏行计数但不猜内容。"""
    p = OUT / f"{uid}_audit.jsonl"
    if not p.exists():
        return [], 0
    kinds: list[str] = []
    ok = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ok += 1
        kinds.append(str(d.get("kind", "?")))
    return kinds, ok


def nlog_prompt(uid: str) -> str:
    p = OUT / f"{uid}_nlog.md"
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("- 需求原文："):
            return line.split("：", 1)[1].strip()
    return ""


def files_of(uid: str) -> dict[str, Path | None]:
    return {
        "yaml": None,  # 由调用方填
        "pkt": OUT / f"{uid}.pkt" if (OUT / f"{uid}.pkt").exists() else None,
        "configs": OUT / f"{uid}_configs.md" if (OUT / f"{uid}_configs.md").exists() else None,
        "report": OUT / f"{uid}_验收报告.md" if (OUT / f"{uid}_验收报告.md").exists() else None,
        "jsonl": OUT / f"{uid}_audit.jsonl" if (OUT / f"{uid}_audit.jsonl").exists() else None,
        "nlog": OUT / f"{uid}_nlog.md" if (OUT / f"{uid}_nlog.md").exists() else None,
        "png": OUT / f"{uid}_topology.png" if (OUT / f"{uid}_topology.png").exists() else None,
    }


def build() -> tuple[str, str, dict]:
    cases = usecases()
    DEST.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    stat = {"n": 0, "full": 0, "partial": 0, "digest": 0, "none": 0, "bad": 0}

    for c in cases:
        uid = c["uid"]
        f = files_of(uid)
        f["yaml"] = c["yaml"]
        kinds, nlines = read_events(uid)
        have = set(kinds)
        missing = [k for k in REQUIRED_EVENTS if k not in have]

        if c["bad"]:
            level = "陷阱"
            stat["bad"] += 1
        elif not kinds and f["nlog"]:
            # 有人读摘要、但没有事件流。摘要里的数字无法逐条回溯到事件。
            level = "仅摘要"
            stat["digest"] += 1
            missing = list(REQUIRED_EVENTS)
        elif not kinds:
            level = "无档"
            stat["none"] += 1
        elif missing:
            level = "部分"
            stat["partial"] += 1
        else:
            level = "完整"
            stat["full"] += 1
        stat["n"] += 1

        rows.append({**c, "f": f, "kinds": kinds, "nlines": nlines,
                     "missing": missing, "level": level,
                     "prompt": c["prompt"] or nlog_prompt(uid)})

        # 落到 out/审计日志/<uid>/
        d = DEST / uid
        d.mkdir(exist_ok=True)
        for key in ("pkt", "configs", "report", "jsonl", "nlog", "png"):
            src = f[key]
            if src:
                shutil.copy2(src, d / src.name)
        shutil.copy2(c["yaml"], d / c["yaml"].name)

    return render_index(rows, stat), render_readme(rows, stat), stat


def render_index(rows: list[dict], stat: dict) -> str:
    L = ["# 审计日志包 · 索引", ""]
    L += [
        "> 本目录由 `python tools/pack_audit.py` 生成，内容全部从 `out/` 与 `examples/` 的",
        "> 现有产物复制而来。**没有补写、没有伪造**：早期运行没留日志的用例，下表直接标「无档」。",
        "",
        "## 0 覆盖情况",
        "",
        "| 用例总数 | 审计链完整 | 部分 | 仅人读摘要 | 无档 | 陷阱用例 |",
        "|---|---|---|---|---|---|",
        f"| {stat['n']} | {stat['full']} | {stat['partial']} | {stat['digest']} "
        f"| {stat['none']} | {stat['bad']} |",
        "",
        "**「完整」的判据**：JSONL 里同时出现 "
        + "、".join(f"`{k}`" for k in REQUIRED_EVENTS)
        + " 五类事件——即「这条产物从需求、到命令筛查、到部署确认、到下发内容、到验证结果」"
        "整条链都留了痕。缺任何一类就是「部分」。",
        "",
        "**「仅人读摘要」**指只剩 `_nlog.md` 这种排版过的摘要、没有事件流：摘要里的数字",
        "（轮数、耗时）无法再逐条回溯到具体事件，所以不能算「可审计」，只能算「有个记录」。",
        "",
        "## 1 用例索引",
        "",
        "| 用例 | 场景 | 需求原文 | DSL | pkt | 配置 | 验收报告 | 审计链 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        f = r["f"]
        mark = {"完整": "✅ 完整",
                "部分": f"⚠️ 部分（缺 {'/'.join(r['missing'])}）",
                "仅摘要": "⚠️ 仅人读摘要",
                "无档": "❌ 无档",
                "陷阱": "—（陷阱用例，不出 pkt）"}[r["level"]]
        prompt = r["prompt"] or "—"
        if len(prompt) > 46:
            prompt = prompt[:46] + "…"
        L.append(
            f"| {r['uid']} | {r['title']} | {prompt} | "
            f"`{r['yaml'].name}` | {'✓' if f['pkt'] else '—'} | "
            f"{'✓' if f['configs'] else '—'} | {'✓' if f['report'] else '—'} | {mark} |"
        )
    L.append("")

    L += ["## 2 每例的文件清单", ""]
    for r in rows:
        d = f"审计日志/{r['uid']}/"
        L += [f"### {r['uid']} {r['title']}", ""]
        if r["prompt"]:
            L += [f"**需求原文**：{r['prompt']}", ""]
        files = [
            ("拓扑 DSL", r["f"]["yaml"]),
            ("交付文件", r["f"]["pkt"]),
            ("逐设备配置", r["f"]["configs"]),
            ("验收报告", r["f"]["report"]),
            ("审计事件流", r["f"]["jsonl"]),
            ("审计摘要", r["f"]["nlog"]),
            ("拓扑图", r["f"]["png"]),
        ]
        L += ["| 类别 | 文件 |", "|---|---|"]
        for label, p in files:
            L.append(f"| {label} | {'`' + d + p.name + '`' if p else '—（无）'} |")
        L.append("")
        if r["kinds"]:
            from collections import Counter
            c = Counter(r["kinds"])
            L.append(
                "事件序列：" + " → ".join(r["kinds"])
                if len(r["kinds"]) <= 10
                else "事件统计：" + "，".join(f"`{k}`×{v}" for k, v in c.items())
            )
            L.append("")
        elif r["level"] == "无档":
            L += [
                "**这一例没有审计档**。它是早期运行留下的产物（审计功能尚未接入），",
                "当时的运行过程没有留下机器可读记录。此处不补写——补写出来的「日志」不是日志。",
                "",
            ]
        elif r["level"] == "仅摘要":
            L += [
                "**这一例只有人读摘要、没有事件流**。摘要里能看到的轮数/耗时无从逐条回溯，",
                "所以不算「可审计」。原始的 JSONL 当时没有落盘，现在补不出来。",
                "",
            ]
        elif r["level"] == "部分":
            L += [
                f"**这一例的审计链是断的**：事件流里缺 `{'`、`'.join(r['missing'])}`。",
                "它是审计功能上线过程中的中间产物——只记了生成阶段，没记下发与验证。",
                "",
            ]
    return "\n".join(L)


def render_readme(rows: list[dict], stat: dict) -> str:
    L = ["# 审计日志包说明", ""]
    L += [
        "## 这里装的是什么",
        "",
        "每个用例一个目录，里面是**同一次生成过程**的全部可查产物：",
        "",
        "| 文件 | 说明 |",
        "|---|---|",
        "| `*.yaml` | 拓扑 DSL —— 模型被判为合格的中间表示，人也能读 |",
        "| `*.pkt` | 交付给 Packet Tracer 的文件（双击可开） |",
        "| `*_configs.md` | 逐设备、逐行的 CLI 配置（粘贴方式的来源） |",
        "| `*_验收报告.md` | 断言 / 一致性检查 / traceroute / 路由表断言的判定结果 |",
        "| `*_audit.jsonl` | 事件流，机器可读，一行一个事件 |",
        "| `*_nlog.md` | 同一事件流的人读版本 |",
        "| `*_topology.png` | 拓扑图 |",
        "",
        "## JSONL 事件类型",
        "",
        "| kind | 含义 | 关键字段 |",
        "|---|---|---|",
        "| `meta` | 本次运行的元信息 | 需求原文、通道、时间 |",
        "| `prompt` | 发给模型的原始需求（NL 通道） | `text` |",
        "| `raw` | 模型原始返回（NL 通道） | `text` |",
        "| `validate` | 本轮 schema + 语义校验结果 | `errors` |",
        "| `guard` | 语义护栏裁决 | `block` / `warn` |",
        "| `round` | 第几轮、耗时、修复了什么 | `elapsed` / `fixed` |",
        "| `cmdscreen` | 命令级筛查（白名单/黑名单） | 配置行数、拦截数、告警数 |",
        "| `confirm` | 部署确认 | **`source`**：`interactive` / `cli` / `static` |",
        "| `deliver` | 每台设备实际下发的配置行 | `devices[]` |",
        "| `verify` | 验收结果 | `pairs` / `reach` / `asserts` / `checks` |",
        "| `done` | 产物落盘 | `pkt` / `png` / `svg` |",
        "",
        "## 怎么顺着查",
        "",
        "拿任何一条报告结论，都能这样追到底：",
        "",
        "1. 在 `*_验收报告.md` 里找到那条断言（例如「PC0→PC2 应不通」）",
        "2. 到 `*_audit.jsonl` 的 `verify` 事件里看它被算成了什么",
        "3. 到 `deliver` 事件里看对应设备的配置行是什么",
        "4. 到 `cmdscreen` 事件里看这些行有没有被白名单/黑名单拦过",
        "5. 到 `confirm` 事件里看 `source` —— 确认是**谁**按的（人手敲的 / 命令行参数 / 静态标记）",
        "",
        "第 5 步是这套日志存在的真正理由：**能证明「没绕过确认门」，而不是嘴上说没绕过。**",
        "",
        "## 已知不足",
        "",
        f"- {stat['n']} 个用例里，审计链完整的只有 **{stat['full']} 个**；"
        f"{stat['partial']} 个事件类型不全、{stat['digest']} 个只剩人读摘要、{stat['none']} 个完全没有档"
        f"（另有 {stat['bad']} 个陷阱用例按设计不出产物）。",
        "  原因是这些运行早于审计功能接入。**缺的没补，也没写「通过」**——"
        "报告里的统计只把有档的算进分母，就是这么来的。",
        "- 审计档**不覆盖**「生成耗时之外的过程」：模型的思考过程、被丢弃的候选方案都没有留。",
        "- `confirm` 的 `source` 只记录「确认来自哪个入口」，**不记录是哪个自然人**——"
        "在单机脚本环境里没有可靠的身份来源，硬记一个反而是假的。",
        "",
    ]
    return "\n".join(L)


def main() -> int:
    index, readme, stat = build()
    (DEST / "索引.md").write_text(index, encoding="utf-8")
    (DEST / "README.md").write_text(readme, encoding="utf-8")
    print(f"已生成：{DEST}")
    print(f"用例 {stat['n']} 个：完整 {stat['full']}、部分 {stat['partial']}、"
          f"仅摘要 {stat['digest']}、无档 {stat['none']}、陷阱 {stat['bad']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
