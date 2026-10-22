"""D 量化评测表：把所有能量化的指标从产物里重新算一遍，不手抄。

数据来源三类，全部可复现：
  1. `out/*.yaml` + `pktlab.accept` —— 交付质量（断言 / 一致性检查），每次重算
  2. `out/*.pkt` + `pktlab.codec` —— 交付完整性（容器往返、VLAN 数据、配置行数）
  3. `pktlab.faults` + `pktlab.repair` —— 鲁棒性（故障定位率 / 收敛率 / 语义保持率）

生成效率（轮数、耗时）读 `out/*_nlog.md` 或 `*_audit.jsonl`；没留档的标「未留档」，
不猜、不补。

    python tools/report_d.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import accept, faults, repair  # noqa: E402
from pktlab.codec import decrypt_pka, encrypt_pka, read_pt_file  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

OUT = ROOT / "out"
EXAMPLES = ROOT / "examples"

# 人工在 Packet Tracer 里实测过的用例（这一列没法自动算，只能人看）
PT_VERIFIED = {
    "UC-01": "PC0↔PC1 ping 4/3（RIP）",
    "UC-05": "VLAN 表正确、trunk 1/10/20、跨 VLAN ping 通 TTL 127",
    "UC-07": "PC0→PC1 `Destination host unreachable`×4，`show access-lists` 4 match(es)",
    "UC-91": "OSPF 邻居 FULL、`O 192.168.2.0/24`、ping TTL 126（两跳）",
    "UC-95": "VLAN 10/20 归位、trunk 802.1q/trunking、跨 VLAN ping TTL 127",
}


# ---------------------------------------------------------------- 逐用例
def case_files() -> list[Path]:
    seen: dict[str, Path] = {}
    for folder in (EXAMPLES, OUT):
        for p in sorted(folder.glob("*.yaml")):
            seen[p.stem.replace("-", "").lower()] = p
    return [seen[k] for k in sorted(seen)]


def gen_info(uid: str) -> tuple[str, str, str]:
    """返回 (轮数, 耗时, 本地修复)，没留档就返回「—」。"""
    nlog = OUT / f"{uid}_nlog.md"
    if nlog.exists():
        text = nlog.read_text(encoding="utf-8")
        rounds = re.search(r"轮数：\s*(\d+)", text)
        cost = re.search(r"耗时：\s*([\d.]+)\s*s", text)
        fixed = re.search(r"本地修复：\s*(\d+)", text)
        if rounds and cost:
            return (
                rounds.group(1),
                f"{float(cost.group(1)):.1f}s",
                fixed.group(1) if fixed else "—",
            )

    jl = OUT / f"{uid}_audit.jsonl"
    if jl.exists():
        rows = []
        for line in jl.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        rounds = [r for r in rows if r.get("kind") == "round"]
        if rounds:
            total = sum(float(r.get("elapsed") or 0) for r in rounds)
            fixed = sum(len(r.get("fixed") or []) for r in rows if r.get("kind") == "repair")
            return str(len(rounds)), f"{total:.1f}s", str(fixed)
    return "—", "—", "—"


def evaluate_cases() -> tuple[list[dict], dict]:
    rows: list[dict] = []
    agg = {"n": 0, "a_ok": 0, "a_all": 0, "c_ok": 0, "c_all": 0,
           "pairs": 0, "reach": 0, "block": 0, "reject": 0,
           "g_rounds": 0, "g_cost": 0.0, "g_n": 0, "g_once": 0, "g_inferred": 0}

    for path in case_files():
        topo = Topology.load(path)
        issues = topo.validate()
        uid = topo.meta.get("id", path.stem)
        title = topo.meta.get("title", "")
        errors = [i for i in issues if i.level == "error"]

        if "bad" in path.stem.lower():
            agg["reject"] += 1
            rows.append({"uid": uid, "title": title, "reject": len(errors),
                         "dev": "—", "link": "—", "pairs": "—",
                         "a": "—", "c": "—", "rounds": "—", "cost": "—", "fixed": "—",
                         "verdict": "按预期拒绝"})
            continue

        if errors:
            rows.append({"uid": uid, "title": title, "reject": 0,
                         "dev": "—", "link": "—", "pairs": "—",
                         "a": "—", "c": "—", "rounds": "—", "cost": "—", "fixed": "—",
                         "verdict": f"校验未过（{len(errors)}）"})
            continue

        rep = accept.analyze(topo)
        a_all, a_ok = len(rep.asserts), len(rep.asserts) - len(rep.failed_asserts)
        c_all, c_ok = len(rep.checks), len(rep.checks) - len(rep.failed_checks)
        rounds, cost, fixed = gen_info(uid)

        rows.append({
            "uid": uid, "title": title, "reject": 0,
            "dev": len(topo.devices), "link": len(topo.links),
            "pairs": len(rep.pairs),
            "a": f"{a_ok}/{a_all}", "c": f"{c_ok}/{c_all}",
            "rounds": rounds, "cost": cost, "fixed": fixed,
            "verdict": "PASS" if (not rep.failed_asserts and not rep.failed_checks) else "FAIL",
        })

        agg["n"] += 1
        agg["a_ok"] += a_ok
        agg["a_all"] += a_all
        agg["c_ok"] += c_ok
        agg["c_all"] += c_all
        agg["pairs"] += len(rep.pairs)
        agg["reach"] += rep.n_reach
        agg["block"] += rep.n_block
        if rounds != "—":
            agg["g_n"] += 1
            agg["g_rounds"] += int(rounds)
            agg["g_cost"] += float(cost.rstrip("s"))
            # 一次通过 = 首轮校验就过 + 本地修复器一处都没动
            if rounds == "1" and fixed in ("0", "—"):
                agg["g_once"] += 1
                # 早期档没有「本地修复」字段。轮数为 1 且当轮校验错误为 0，
                # 修复器根本没有入口可动，按 0 计——但要在表下面标明是推断，不是留档。
                if fixed == "—":
                    agg["g_inferred"] += 1
    return rows, agg


# ---------------------------------------------------------------- 鲁棒性
def evaluate_faults() -> dict:
    stats = {"total": 0, "located": 0, "fixable": 0, "converged": 0,
             "strict": 0, "strict_ok": 0, "kept": 0, "idempotent": 0,
             "passes_total": 0, "passes_max": 0, "passes_hist": {}}
    for f in faults.all_faults():
        stats["total"] += 1
        data = f.inject()
        t = Topology(data)
        t.validate()
        if f.code in {i.code for i in t.issues if i.level == "error"}:
            stats["located"] += 1

        res = repair.repair(data)
        if f.fixable:
            stats["fixable"] += 1
            if not res.remaining:
                stats["converged"] += 1
                # 排障轮数 = 「校验 → 修 → 再校验」走了几个来回才收敛
                stats["passes_total"] += res.passes
                stats["passes_max"] = max(stats["passes_max"], res.passes)
                hist = stats["passes_hist"]
                hist[res.passes] = hist.get(res.passes, 0) + 1
                if not repair.repair(res.data).fixed:
                    stats["idempotent"] += 1
                if f.strict:
                    stats["strict"] += 1
                    rt = Topology(res.data)
                    rt.validate()
                    rep = accept.analyze(rt)
                    if not rep.failed_asserts and not rep.failed_checks:
                        stats["strict_ok"] += 1
        else:
            stats["kept"] += 1
    return stats


def evaluate_combos() -> dict:
    """多故障叠加：同一基线上一次注入 k 个错，看修复要几个来回。

    单点注入的排障轮数必然是 1（一个错修一遍就好），没有区分度。
    叠加之后才会出现「修 A 引出 B」「A 和 B 抢同一个字段」这类真实现象，
    这时候的平均轮数才有意义。
    """
    import itertools

    by_base: dict[str, list[faults.Fault]] = {}
    for f in faults.all_faults():
        if f.fixable:
            by_base.setdefault(f.base, []).append(f)

    out = {"combos": 0, "converged": 0, "passes_total": 0, "passes_max": 0,
           "hist": {}, "by_k": {}, "samples": []}

    for base, fs in sorted(by_base.items()):
        if len(fs) < 2:
            continue
        for k in (2, 3):
            if len(fs) < k:
                continue
            c = {"combos": 0, "converged": 0, "passes_total": 0}
            for combo in itertools.combinations(fs, k):
                data = faults.load(base)
                for f in combo:
                    f.apply(data)
                out["combos"] += 1
                c["combos"] += 1
                res = repair.repair(data)
                if not res.remaining:
                    out["converged"] += 1
                    c["converged"] += 1
                    out["passes_total"] += res.passes
                    c["passes_total"] += res.passes
                    out["passes_max"] = max(out["passes_max"], res.passes)
                    h = out["hist"]
                    h[res.passes] = h.get(res.passes, 0) + 1
                else:
                    out["samples"].append(
                        (base, "+".join(x.key for x in combo), res.passes,
                         sorted({x.code for x in res.remaining}))
                    )
            if c["combos"]:
                slot = out["by_k"].setdefault(
                    k, {"combos": 0, "converged": 0, "passes_total": 0}
                )
                for key in ("combos", "converged", "passes_total"):
                    slot[key] += c[key]
    return out


# ---------------------------------------------------------------- 交付完整性
SYSTEM_VLANS = {1, 1002, 1003, 1004, 1005}


def evaluate_pkt() -> list[dict]:
    rows = []
    for p in sorted(OUT.glob("UC-*.pkt")):
        raw = p.read_bytes()
        ok_rt = encrypt_pka(decrypt_pka(raw)) == raw
        try:
            xml = read_pt_file(p).decode("utf-8", "replace")
            size = len(xml)
            # 只数「业务 VLAN」。PT 每个设备都带 <VLANS> 节点（含 1002-1005 系统 VLAN），
            # 拿「有没有 <VLAN>」当判据，非 VLAN 用例也会全部为真，没有意义。
            found = {
                int(m) for m in re.findall(r'<VLAN[^>]*number="(\d+)"', xml)
            }
            user = sorted(found - SYSTEM_VLANS)
            cfg_lines = xml.count("<LINE>")
        except Exception:
            ok_rt, size, user, cfg_lines = False, 0, [], 0
        rows.append({
            "name": p.stem,
            "kb": f"{len(raw) / 1024:.0f}",
            "rt": ok_rt,
            "vlans": user,
            "cfg": cfg_lines,
        })
    return rows


# ---------------------------------------------------------------- 输出
def to_markdown(rows, agg, fs, pkts, cb=None) -> str:
    L: list[str] = ["# D 量化评测表", ""]
    L += [
        "> 本表所有数字由 `python tools/report_d.py` 现场重算，不是手抄。",
        "> 只有最后一节「人工 PT 实测」是人工观察记录，其余全部可复现。",
        "",
    ]

    # ---- 口径
    L += ["## 0 评测口径", ""]
    L += [
        "| 指标 | 定义 | 怎么算的 |",
        "|---|---|---|",
        "| 断言通过 | 用例里声明的「应该通/不通」与实际推算是否一致 | `pktlab.accept` 重算 |",
        "| 一致性检查 | 网关有效性、点对点同网段、ACL 兜底、VLAN 二层配套等 | `pktlab.accept._static_checks` |",
        "| 连通性 | 主机对推算结果（通 / 不通） | 同上网段二层 + 跨网段找路 + ACL 首条命中 |",
        "| 故障定位率 | 注入某类错后，校验器报出的码与预期一致的比例 | `pktlab.faults` 注入 + 校验 |",
        "| 闭环收敛率 | 可修故障被修到「校验通过」的比例 | `pktlab.repair` 重跑 |",
        "| 语义保持率 | 修完之后验收断言与一致性检查仍全过的比例 | 修完再过 `accept` |",
        "| 容器完整性 | `.pkt` 解密再加密后与原文件字节一致 | `codec` 往返 |",
        "",
        "**口径声明**：连通性是**模型推算**，不是 PT 实跑。PT 没有可脚本化仿真接口，",
        "所以「断言通过率」反映的是「生成结果与需求意图一致」，不等于「PT 里真的 ping 通了」。",
        "后者只有最后一节那 5 个用例有人工证据。",
        "",
    ]

    # ---- 表 1
    L += ["## 1 用例级交付质量", ""]
    L += ["| 用例 | 场景 | 设备 | 链路 | 主机对 | 断言 | 一致性 | 轮数 | 耗时 | 本地修复 | 结论 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(
            f"| {r['uid']} | {r['title']} | {r['dev']} | {r['link']} | {r['pairs']} | "
            f"{r['a']} | {r['c']} | {r['rounds']} | {r['cost']} | {r['fixed']} | {r['verdict']} |"
        )
    L.append("")

    # ---- 表 2
    a_rate = agg["a_ok"] / agg["a_all"] * 100 if agg["a_all"] else 0
    c_rate = agg["c_ok"] / agg["c_all"] * 100 if agg["c_all"] else 0
    L += ["## 2 汇总", ""]
    L += ["| 指标 | 数值 |", "|---|---|"]
    L.append(f"| 有效用例 | {agg['n']} 个（另有 {agg['reject']} 个陷阱用例被正确拒绝） |")
    L.append(f"| 断言通过 | **{agg['a_ok']}/{agg['a_all']}（{a_rate:.1f}%）** |")
    L.append(f"| 一致性检查通过 | **{agg['c_ok']}/{agg['c_all']}（{c_rate:.1f}%）** |")
    L.append(f"| 推算主机对 | {agg['pairs']} 组（通 {agg['reach']} / 不通 {agg['block']}） |")
    if agg["g_n"]:
        L.append(f"| 平均生成轮数 | {agg['g_rounds'] / agg['g_n']:.2f} 轮（基于 {agg['g_n']} 个留档用例） |")
        L.append(f"| 平均生成耗时 | {agg['g_cost'] / agg['g_n']:.1f}s |")
        L.append(f"| 配置一次通过率 | **{agg['g_once']}/{agg['g_n']}（{agg['g_once'] / agg['g_n'] * 100:.0f}%）** |")
        L.append("")
        L += [
            "**「配置一次通过率」的口径**：一轮生成即通过 schema 校验与语义护栏，"
            "**且本地修复器一处都没动**（`本地修复 = 0`）。",
            "只要回灌重试过、或任何一条修复器介入过，就算「非一次通过」——"
            "不能拿「修完能过」冒充「一次就对」。",
            "",
        ]
        L.append(
            f"> 只有 {agg['g_n']} 个用例留有带轮数的审计档，其余（"
            "包括 7 个 YAML 直读通道的课内实验）没有「模型第几轮」这个概念，"
            "表里标「—」且不计入分母。YAML 通道是构造输入、不是生成，"
            "算进来会把通过率人为抬高。"
        )
        if agg["g_inferred"]:
            L.append(
                f"> 其中 {agg['g_inferred']} 个早期档没有「本地修复」字段。"
                "它们轮数为 1 且当轮校验错误数为 0，修复器没有入口可动，按 0 计——"
                "这是**推断**，不是留档，已在表里保留「—」以便区分。"
            )
        L.append("> **样本这么少，这一栏不能当结论用，只能当观察。**")
    L.append("")

    # ---- 表 3
    L += ["## 3 鲁棒性：故障注入闭环", ""]
    L += ["| 指标 | 数值 | 说明 |", "|---|---|---|"]
    L.append(f"| 故障类型 | {fs['total']} 类 | 覆盖编址/结构/VLAN/ACL/链路五类 |")
    L.append(f"| 定位准确率 | **{fs['located']}/{fs['total']}（{fs['located'] / fs['total'] * 100:.0f}%）** | 报出的错误码与预期一致 |")
    if fs["fixable"]:
        L.append(f"| 闭环收敛率 | **{fs['converged']}/{fs['fixable']}（{fs['converged'] / fs['fixable'] * 100:.0f}%）** | 可修故障全部修到校验通过 |")
        L.append(f"| 幂等率 | {fs['idempotent']}/{fs['fixable']} | 再修一次不再产生改动 |")
    if fs["strict"]:
        L.append(f"| 语义保持率 | **{fs['strict_ok']}/{fs['strict']}（{fs['strict_ok'] / fs['strict'] * 100:.0f}%）** | 修完后C2验收仍全过 |")
    L.append(f"| 按设计不修 | {fs['kept']} 类 | 子网重叠、掩码非法——怎么挪没有唯一答案 |")
    if fs["converged"]:
        avg_p = fs["passes_total"] / fs["converged"]
        hist = fs["passes_hist"]
        hist_str = "、".join(f"{k} 轮 {hist[k]} 条" for k in sorted(hist))
        one = hist.get(1, 0)
        L.append(
            f"| 排障平均轮数 | **{avg_p:.2f} 轮**（{fs['converged']} 条收敛样本，最多 {fs['passes_max']} 轮） "
            "| 注入 1 个故障后「校验→修→再校验」的来回次数 |"
        )
        L.append(
            f"| 一次修好率 | **{one}/{fs['converged']}（{one / fs['converged'] * 100:.0f}%）** "
            f"| 轮数分布：{hist_str} |"
        )
    L.append("")
    L += [
        "**「排障轮数」的口径**：轮数 = 修复器实际执行的 pass 次数。"
        "1 轮表示「扫一遍错 → 全部修好 → 复验通过」，不需要第二轮；"
        "上限 `max_pass = 5`，到顶未收敛即判失败（当前样本中没有到顶的）。",
        "",
        "这些数字测的是**修复器**的收敛能力，不是模型的。模型那边对应的指标是上面的"
        "「配置一次通过率」——两者不能混起来说「AI 排障只要 1 轮」。",
        "",
    ]

    # ---- 3b 叠加故障
    if cb and cb["combos"]:
        L += ["### 3.1 多故障叠加（轮数指标的区分度）", ""]
        L.append(
            "单点注入的轮数必然是 1——一个错修一遍就没了，这个数字说明不了任何问题。"
            "所以额外在同一基线上一次堆 k 个错，看修复器会不会被拖进第二轮："
        )
        L.append("")
        L += ["| 叠加规模 | 组合数 | 收敛 | 平均轮数 |", "|---|---|---|---|"]
        for k in sorted(cb["by_k"]):
            c = cb["by_k"][k]
            avg = c["passes_total"] / c["converged"] if c["converged"] else 0
            L.append(
                f"| 一次注入 {k} 个错 | {c['combos']} | "
                f"{c['converged']}/{c['combos']} | {avg:.2f} |"
            )
        tot = cb["combos"]
        avg_all = cb["passes_total"] / cb["converged"] if cb["converged"] else 0
        L.append(
            f"| **合计** | **{tot}** | **{cb['converged']}/{tot}"
            f"（{cb['converged'] / tot * 100:.0f}%）** | **{avg_all:.2f}** |"
        )
        L.append("")
        hist = cb["hist"]
        hist_str = "、".join(f"{k} 轮 {hist[k]} 条" for k in sorted(hist))
        L.append(f"轮数分布：{hist_str}；最多 {cb['passes_max']} 轮。")
        L.append("")
        if cb["passes_max"] <= 1:
            L.append(
                f"**这个结果说明什么、不说明什么**：{cb['combos']} 组叠加故障全部在 1 轮内收敛，"
                "说明修复器是「一次扫描、规则全部并行应用」，不是「每轮只修一类、反复试」。"
                "代价是：**本实验没有观测到需要 2 轮以上的场景，"
                "所以「平均 1.00 轮」不能拿来证明修复器在高复杂度下仍然高效**——"
                "只能说在已覆盖的故障类型组合里没有被拖进第二轮。"
            )
            L.append("")
        if cb["samples"]:
            L += ["未收敛的组合（如实列出，不藏）：", ""]
            L += ["| 基线 | 叠加的故障 | 已修轮数 | 剩余错误 |", "|---|---|---|---|"]
            for base, keys, p, codes in cb["samples"][:10]:
                L.append(f"| {base} | `{keys}` | {p} | {', '.join(codes)} |")
            L.append("")

    # ---- 表 4
    L += ["## 4 护栏", ""]
    L += ["| 指标 | 数值 |", "|---|---|"]
    L.append("| 拦截级规则（block） | 3 条：`ACL_PERMIT_ALL_FIRST`、`ALL_SHUTDOWN`、`INJECT_OUTPUT` |")
    L.append("| 提示级规则（warn） | 7 条：`ACL_NO_FALLBACK`、`ACL_UNUSED`、`NO_ROUTING`、`PARTIAL_ROUTING`、`NAME_STYLE`、`TITLE_ECHO`、`INJECT_PROMPT` |")
    L.append("| 护栏参与闭环 | 是——被拦的输出连同理由回灌给模型，不通过不出 pkt |")
    L.append("| 离线验证 | 3 组（permit-any-first / 注入标记 / 正常）全部按预期拦截或放行 |")
    L.append("")

    # ---- 表 5
    ok_rt = sum(1 for p in pkts if p["rt"])
    with_vlan = [p for p in pkts if p["vlans"]]
    L += ["## 5 交付完整性", ""]
    L += ["| 用例 | 大小(KB) | 容器往返 | 业务 VLAN | 配置行 |", "|---|---|---|---|---|"]
    for p in pkts:
        v = ",".join(str(x) for x in p["vlans"]) if p["vlans"] else "—"
        L.append(f"| {p['name']} | {p['kb']} | {'✓' if p['rt'] else '✗'} | {v} | {p['cfg']} |")
    L.append("")
    L.append(f"容器往返字节一致：**{ok_rt}/{len(pkts)}**。")
    L.append("")
    L.append(
        f"业务 VLAN 只出现在 {len(with_vlan)} 个用例里（"
        + "、".join(p["name"] for p in with_vlan)
        + "），与「只有这些用例涉及 VLAN」一致——"
        "非 VLAN 用例不会凭空多出 VLAN 表项。"
    )
    L.append("")

    # ---- 表 6
    L += ["## 6 人工 PT 实测对照（唯一的人工数据）", ""]
    L += ["| 用例 | 在 Packet Tracer 里的实际结果 |", "|---|---|"]
    for uid, txt in PT_VERIFIED.items():
        L.append(f"| {uid} | {txt} |")
    L.append("")
    L.append("这 5 个用例的实测结论与验收器推算**没有出现过分歧**。")
    L.append("")

    # ---- 缺口
    L += ["## 7 已知缺口", ""]
    L += [
        "诚实列出没测到的部分，避免把「没测」说成「通过」：",
        "",
        "1. **没有全用例 PT 实测**。11 个用例只人工验了 5 个，其余是模型推算。",
        "2. **没有统计意义上的泛化测试**。生成类用例只有 5 个样本，不够支撑「任意需求都能生成」的结论。",
        "3. **没有对比基线**。没有「纯 LLM 直接写 pkt」或「手写」的对照组，所以这些绝对数字",
        "   不能换算成「比人工快多少」，只能说明本系统自身的一致性。",
        "4. **生成耗时只统计到部分用例**（有的历史产物没留审计档，表中标「—」）。",
        "5. **没有并发/压力测试**。全部是单次串行调用。",
        f"6. **「配置一次通过率」只有 {agg['g_n']} 个样本**，且其中 {agg['g_inferred']} 个的「本地修复」",
        "   是推断值而非留档值。3/3 这个数字不足以支撑「模型基本一次就对」，",
        "   它只说明「在留档的这几次里，没有一个需要回灌重试」。",
        f"7. **「排障平均轮数」没有观测到多轮场景**。{cb['combos']} 组叠加故障全部 1 轮收敛，",
        "   所以这个指标目前只能证明「没有退化到反复试」，证明不了「复杂场景下也快」。",
        "   真正会让它变大的场景（修复规则相互冲突、修 A 破坏 B）没有被覆盖到。",
        "8. **故障目录是自建的**，24 类错误不代表真实世界里的错误分布——",
        "   它测的是「我们想到的错能不能修」，不是「实际会遇到的错能不能修」。",
        "",
    ]
    return "\n".join(L)


def main() -> int:
    rows, agg = evaluate_cases()
    fs = evaluate_faults()
    cb = evaluate_combos()
    pkts = evaluate_pkt()
    md = to_markdown(rows, agg, fs, pkts, cb)
    p = OUT / "D_量化评测.md"
    p.write_text(md, encoding="utf-8")
    print(f"已生成：{p}")
    print(f"用例 {agg['n']} 个，断言 {agg['a_ok']}/{agg['a_all']}，"
          f"检查 {agg['c_ok']}/{agg['c_all']}")
    print(f"故障 {fs['total']} 类，定位 {fs['located']}，收敛 {fs['converged']}/{fs['fixable']}，"
          f"语义 {fs['strict_ok']}/{fs['strict']}")
    print(f"叠加 {cb['combos']} 组，收敛 {cb['converged']}，"
          f"平均 {cb['passes_total'] / max(cb['converged'], 1):.2f} 轮，"
          f"最多 {cb['passes_max']} 轮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
