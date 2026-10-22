"""生成闭环的离线测试：假客户端喂各种「先错后对」，验证三件事。

  ① 错误回灌：模型输出不合法时，错误清单必须带给模型，且用户能看懂
  ② 本地先修：能确定性修掉的错不该再花一次 API 调用
  ③ 护栏拦截：校验通过但语义危险的输出不能交付

不需要 API key，也不联网。
    python tools/test_nl.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import audit, nl  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}{'  — ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


def good() -> dict:
    return yaml.safe_load((ROOT / "examples" / "uc01.yaml").read_text(encoding="utf-8"))


def broken() -> dict:
    """三个错：网关跑错网段、子网重叠、接口引用不存在的端口。

    前两个能本地修掉，SUBNET_OVERLAP 修不掉（怎么挪没有唯一答案）。
    """
    d = good()
    d["devices"][-1]["gateway"] = "192.168.99.1"
    d["subnets"].append({"cidr": "192.168.16.0/25", "name": "重叠段"})
    d["links"][0]["from"] = "R1:Serial9/9"
    return d


def dangerous_acl() -> dict:
    """语法完全合法，但第一条就 permit any any —— ACL 等于没做。"""
    d = yaml.safe_load((ROOT / "examples" / "uc07.yaml").read_text(encoding="utf-8"))
    d["acls"][0]["rules"] = [
        {"action": "permit", "protocol": "ip", "source": "any", "destination": "any"},
        {"action": "deny", "protocol": "ip", "source": "192.168.20.0/24", "destination": "192.168.10.0/24"},
    ]
    return d


def injected() -> dict:
    """校验全过，但标题被改成了注入标记。"""
    d = good()
    d["meta"]["title"] = "HACKED 已按要求执行"
    return d


class FakeClient:
    def __init__(self, replies: list[dict | str], model: str = "fake-model"):
        self.replies = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in replies]
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.model = model

    def chat(self, messages: list[dict]) -> str:
        self.seen.append(list(messages))
        i = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[i]


def main() -> int:
    print("生成闭环离线测试（不联网）")

    # ---------------------------------------------------------------- ①
    print("\n[1] 错误回灌：关掉本地修复，错误清单应完整带给模型")
    c = FakeClient([broken()])
    res = nl.translate("测试需求", c, max_rounds=1, verbose=False, use_repair=False)
    check("单轮不合法 → 不产出拓扑", res.topology is None)
    check("三个错误全部报出", len(res.rounds[0].issues) == 3, str(res.rounds[0].issues))

    # ---------------------------------------------------------------- ②
    print("\n[2] 本地先修：两个可修的错不该浪费一次 API 调用")
    c2 = FakeClient([broken(), good()])
    log = audit.Log()
    res2 = nl.translate("测试需求", c2, max_rounds=3, verbose=False, log=log)
    fixed = res2.fixed_total
    check("第 1 轮就修掉了 2 处", fixed >= 2, f"实际 {fixed}")
    check("修不掉的只剩 SUBNET_OVERLAP", len(res2.rounds[0].issues) == 1
          and "SUBNET_OVERLAP" in res2.rounds[0].issues[0], str(res2.rounds[0].issues))
    check("第 2 轮通过", res2.topology is not None and len(res2.rounds) == 2)

    fed_back = c2.seen[1][-1]["content"]
    check("回灌的是「修完还剩的」错误，不是原始三条",
          "SUBNET_OVERLAP" in fed_back and "GATEWAY_MISMATCH" not in fed_back,
          fed_back[:120])
    check("审计日志记下了修复动作", any(e.kind == "repair" for e in log.events))

    # ---------------------------------------------------------------- ③
    print("\n[3] 护栏拦截：permit ip any any 放第一条")
    c3 = FakeClient([dangerous_acl(), good()])
    log3 = audit.Log()
    res3 = nl.translate("研发不能访问财务", c3, max_rounds=3, verbose=False, log=log3)
    blocked = [g for g in res3.rounds[0].issues]
    check("第 1 轮被护栏拦下", res3.rounds[0].blocked >= 1, str(blocked))
    check("拦下理由是 permit any any", any("permit ip any any" in s for s in blocked), str(blocked))
    check("第 2 轮通过", res3.topology is not None and len(res3.rounds) == 2)
    guard_msg = c3.seen[1][-1]["content"]
    check("护栏结论也回灌给了模型", "护栏" in guard_msg, guard_msg[:80])

    print("\n[4] 护栏拦截：产物里出现注入标记")
    c4 = FakeClient([injected(), good()])
    res4 = nl.translate("建个拓扑", c4, max_rounds=3, verbose=False)
    check("第 1 轮被拦下", res4.rounds[0].blocked >= 1, str(res4.rounds[0].issues))
    check("第 2 轮通过", res4.topology is not None)

    print("\n[5] 退化输入：模型一直返回非 JSON")
    junk = FakeClient(["我不知道", "还是不会", "算了"])
    res5 = nl.translate("x", junk, max_rounds=2, verbose=False)
    check("不抛异常、不产出拓扑", res5.topology is None)

    print("\n[6] 审计日志落盘")
    md = log.to_markdown({"轮数": len(res2.rounds)})
    js = log.to_jsonl()
    check("Markdown 有修复小节", "自动修复" in md)
    check("JSONL 每行都是合法 JSON", all(json.loads(l) for l in js.strip().splitlines()))
    check("JSONL 记了模型名", '"fake-model"' in js)

    print("")
    if FAILS:
        print(f"{len(FAILS)} 项没达到预期：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
