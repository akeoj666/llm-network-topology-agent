"""自然语言 → 拓扑 DSL（DeepSeek）。

设计要点（对应评分标准 A1 / A2 / B2 / C3 / E）：
  1. **约束外置**：把「Packet Tracer 硬件端口」「骨架容量」「编址规则」写成提示词，
     不让模型自由发挥。这些规则全部来自实测踩坑，不是想当然。
  2. **结构化输出**：DeepSeek 走 json_object 模式，输出必须是单个 JSON 对象。
  3. **本地先修，再回灌**：生成结果先过 `repair`，能确定性修掉的错（网关跑出网段、
     子接口编号与 dot1q 不一致、标准 ACL 里写了目的地址……）就地改掉，不再调一次 API。
     修不掉的（子网重叠、掩码非法）才把错误清单原样回灌给模型重写，最多 max_rounds 轮。
     这一步是「生成合格性」的关键——实测 PT 回放配置遇第一行非法命令就中止。
  4. **护栏拦截**：校验通过不等于能交付。ACL 第一条就 `permit ip any any`、
     整台设备接口全 shutdown、产物里出现需求夹带的指令词——这些 schema 查不出来，
     由 `audit.guard` 拦下并回灌，不通过就不出 pkt。
  5. **可审计**：每一轮的输入输出、修复动作、护栏裁决都记进 `audit.Log`，
     落盘成 JSONL + Markdown 供报告引用。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import audit, repair
from .dsl import Topology

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

SYSTEM = """你是一个网络拓扑编译器。用户用中文描述一个 Packet Tracer 组网需求，
你把它翻译成一个 JSON 对象（拓扑 DSL）。只输出这个 JSON 对象本身：
不要 markdown 代码块，不要解释文字，不要注释。

## 顶层字段
{
  "meta":     {"id": "UC-数字两位", "title": "中文短标题", "source_prompt": "用户原话"},
  "devices":  [ ... ],            // 2-20 台
  "links":    [ ... ],            // 至少 1 条
  "subnets":  [ ... ],            // 网段清单
  "vlans":    [ ... ],            // 只有 VLAN 场景才写
  "routing":  [ ... ]             // 每台路由器一条
}

## 各字段
devices[]:
  {"name": "R1", "role": "router|switch|pc|server", "model": "...",
   "ip": "192.168.1.10/24",        // 仅主机
   "gateway": "192.168.1.1",       // 仅主机
   "interfaces": [ ... ]}          // 路由器/交换机必写

interfaces[]:
  {"name": "Serial0/0", "ip": "10.0.0.1/30",
   "dot1q": 10,                    // 仅在路由器子接口上用
   "area": 0,                      // 仅 OSPF 多区域时用
   "vlan": 10, "mode": "access|trunk",   // 仅交换机
   "shutdown": true}               // 不用的接口标 true

links[]: {"from": "R1:Serial0/0", "to": "R2:Serial1/0",
          "cable": "serial|copper|cross", "subnet": "10.0.0.0/30"}

subnets[]: {"cidr": "192.168.1.0/24", "name": "admin",
            "gateway": "192.168.1.1", "hosts": 1, "vlan": 10}

vlans[]: {"id": 10, "name": "RD", "cidr": "192.168.10.0/24"}

routing[]: {"device": "R1", "protocol": "rip|ospf|static|bgp|none",
            "version": 2, "auto_summary": false,      // rip
            "process": 1, "area": 0, "stub_areas": [100],  // ospf
            "default_route": "10.0.0.2"}              // static

acls[]: {"id": 100, "type": "extended|standard",
         "rules": [{"action": "permit|deny",
                    "protocol": "ip|tcp|udp|icmp",   // 仅扩展 ACL
                    "source": "any|192.168.1.0/24|10.1.1.5/32",
                    "destination": "同上",           // 仅扩展 ACL
                    "port": 80}],                    // 仅 tcp/udp
         "apply": [{"device": "R1", "interface": "FastEthernet2/0",
                    "direction": "in|out"}]}

## 硬约束（违反任何一条，Packet Tracer 里设备会配置中断）
1. 路由器一律 "Router-PT-Empty"。它的端口**只有** Serial0/0、Serial1/0、
   FastEthernet2/0、FastEthernet3/0。**没有 FastEthernet0/0**，不要写。
2. 交换机一律 "2960-24TT"，端口 FastEthernet0/1 ~ FastEthernet0/24、
   GigabitEthernet0/1、GigabitEthernet0/2。上联口固定用 FastEthernet0/24。
3. PC 一律 "PC-PT"，只有 FastEthernet0。
4. 每台路由器/交换机用到的接口都要在 interfaces 里显式列出（生成器整段替换配置）。
   空着不用的接口请写 {"name": "...", "shutdown": true}，不要省略。
5. 骨架容量上限：路由器 ≤ 3 台，交换机 ≤ 3 台，PC/服务器 ≤ 6 台。
6. 路由器之间用串行链路（cable: "serial"），网段用 /30；主机与交换机之间用 "copper"。
7. 主机必须写 gateway，且网关必须落在自己网段内，不能是网段号、广播地址或主机自身地址。
8. 所有子网之间不得重叠；如果给了 pool，所有子网必须落在 pool 内。
9. VLAN 场景：vlans 里声明 {id, name, cidr}；交换机接入口写 {"mode":"access","vlan":N}，
   上联口写 {"mode":"trunk"}。单臂路由要在路由器上同时写父接口
   {"name":"FastEthernet2/0"} 和子接口 {"name":"FastEthernet2/0.10","ip":"...","dot1q":10}，
   子接口编号必须与 dot1q 一致。
10. links 里的端口名必须与 devices 里声明的接口名完全一致。
11. ACL（安全部署）：
    - 编号：标准 ACL 1-99，**只能按源地址过滤**（不写 protocol / destination）；
      扩展 ACL 100-199，可写 protocol / source / destination / port。
    - 只能挂在**路由器**的三层接口上（`apply.device` 必须是 router），
      接口必须在该设备的 interfaces 里声明过。
    - 规则要写全：最后一条通常是 `permit ip any any` 兜底，
      否则 ACL 末尾隐含 deny all，会把没列到的流量全掐掉。
    - 需求里出现「禁止 / 不允许 / 限制 / 隔离 / 访问控制 / 只能访问」就要写 acls。

## 惯例
- 网关默认取子网第一个可用地址（.1）；若用户明确要求用最后一个可用地址则照办。
- links.subnet 填这条链路所在的网段，会作为画布标注显示。
- routing 里没写的路由器默认 protocol: "none"。
"""

FEWSHOT = [
    {
        "role": "user",
        "content": "两台路由器用一条串口线连起来，左边接 192.168.1.0/24 给行政部，"
        "右边接 192.168.2.0/24 给研发部，各挂一台交换机和一台 PC，跑 OSPF。",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "meta": {"id": "UC-90", "title": "双路由 OSPF", "source_prompt": "……"},
                "subnets": [
                    {"cidr": "10.0.0.0/30", "name": "R1-R2"},
                    {"cidr": "192.168.1.0/24", "name": "admin", "gateway": "192.168.1.1", "hosts": 1},
                    {"cidr": "192.168.2.0/24", "name": "rd", "gateway": "192.168.2.1", "hosts": 1},
                ],
                "devices": [
                    {
                        "name": "R1", "role": "router", "model": "Router-PT-Empty",
                        "interfaces": [
                            {"name": "FastEthernet2/0", "ip": "192.168.1.1/24"},
                            {"name": "Serial0/0", "ip": "10.0.0.1/30"},
                            {"name": "Serial1/0", "shutdown": True},
                            {"name": "FastEthernet3/0", "shutdown": True},
                        ],
                    },
                    {
                        "name": "R2", "role": "router", "model": "Router-PT-Empty",
                        "interfaces": [
                            {"name": "FastEthernet2/0", "ip": "192.168.2.1/24"},
                            {"name": "Serial1/0", "ip": "10.0.0.2/30"},
                            {"name": "Serial0/0", "shutdown": True},
                            {"name": "FastEthernet3/0", "shutdown": True},
                        ],
                    },
                    {
                        "name": "SW0", "role": "switch", "model": "2960-24TT",
                        "interfaces": [
                            {"name": "FastEthernet0/1"}, {"name": "FastEthernet0/24"},
                        ],
                    },
                    {
                        "name": "SW1", "role": "switch", "model": "2960-24TT",
                        "interfaces": [
                            {"name": "FastEthernet0/1"}, {"name": "FastEthernet0/24"},
                        ],
                    },
                    {"name": "PC0", "role": "pc", "model": "PC-PT",
                     "ip": "192.168.1.10/24", "gateway": "192.168.1.1"},
                    {"name": "PC1", "role": "pc", "model": "PC-PT",
                     "ip": "192.168.2.10/24", "gateway": "192.168.2.1"},
                ],
                "links": [
                    {"from": "R1:Serial0/0", "to": "R2:Serial1/0", "cable": "serial",
                     "subnet": "10.0.0.0/30"},
                    {"from": "PC0:FastEthernet0", "to": "SW0:FastEthernet0/1", "cable": "copper"},
                    {"from": "SW0:FastEthernet0/24", "to": "R1:FastEthernet2/0", "cable": "copper"},
                    {"from": "PC1:FastEthernet0", "to": "SW1:FastEthernet0/1", "cable": "copper"},
                    {"from": "SW1:FastEthernet0/24", "to": "R2:FastEthernet2/0", "cable": "copper"},
                ],
                "routing": [
                    {"device": "R1", "protocol": "ospf", "process": 1, "area": 0},
                    {"device": "R2", "protocol": "ospf", "process": 1, "area": 0},
                ],
            },
            ensure_ascii=False,
        ),
    },
    # 第二个例子专门给 ACL：安全部署是评分重点，模型最容易在这里漏兜底规则
    {
        "role": "user",
        "content": "两台路由器串口相连，各带一台交换机和一台 PC，研发部 192.168.10.0/24，"
        "财务部 192.168.20.0/24，要求研发部不能访问财务部，其他流量放行，用 OSPF 和 ACL 实现。",
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "meta": {"id": "UC-91", "title": "ACL 部门隔离", "source_prompt": "……"},
                "subnets": [
                    {"cidr": "10.0.0.0/30", "name": "R1-R2"},
                    {"cidr": "192.168.10.0/24", "name": "rd", "gateway": "192.168.10.1", "hosts": 1},
                    {"cidr": "192.168.20.0/24", "name": "fin", "gateway": "192.168.20.1", "hosts": 1},
                ],
                "devices": [
                    {"name": "R1", "role": "router", "model": "Router-PT-Empty",
                     "interfaces": [
                         {"name": "FastEthernet2/0", "ip": "192.168.10.1/24"},
                         {"name": "Serial0/0", "ip": "10.0.0.1/30"},
                         {"name": "Serial1/0", "shutdown": True},
                         {"name": "FastEthernet3/0", "shutdown": True}]},
                    {"name": "R2", "role": "router", "model": "Router-PT-Empty",
                     "interfaces": [
                         {"name": "FastEthernet2/0", "ip": "192.168.20.1/24"},
                         {"name": "Serial1/0", "ip": "10.0.0.2/30"},
                         {"name": "Serial0/0", "shutdown": True},
                         {"name": "FastEthernet3/0", "shutdown": True}]},
                    {"name": "SW0", "role": "switch", "model": "2960-24TT",
                     "interfaces": [{"name": "FastEthernet0/1"}, {"name": "FastEthernet0/24"}]},
                    {"name": "SW1", "role": "switch", "model": "2960-24TT",
                     "interfaces": [{"name": "FastEthernet0/1"}, {"name": "FastEthernet0/24"}]},
                    {"name": "PC0", "role": "pc", "model": "PC-PT",
                     "ip": "192.168.10.10/24", "gateway": "192.168.10.1"},
                    {"name": "PC1", "role": "pc", "model": "PC-PT",
                     "ip": "192.168.20.10/24", "gateway": "192.168.20.1"},
                ],
                "links": [
                    {"from": "R1:Serial0/0", "to": "R2:Serial1/0", "cable": "serial",
                     "subnet": "10.0.0.0/30"},
                    {"from": "PC0:FastEthernet0", "to": "SW0:FastEthernet0/1", "cable": "copper"},
                    {"from": "SW0:FastEthernet0/24", "to": "R1:FastEthernet2/0", "cable": "copper"},
                    {"from": "PC1:FastEthernet0", "to": "SW1:FastEthernet0/1", "cable": "copper"},
                    {"from": "SW1:FastEthernet0/24", "to": "R2:FastEthernet2/0", "cable": "copper"},
                ],
                "routing": [
                    {"device": "R1", "protocol": "ospf", "process": 1, "area": 0},
                    {"device": "R2", "protocol": "ospf", "process": 1, "area": 0},
                ],
                "acls": [
                    {"id": 100, "type": "extended",
                     "rules": [
                         {"action": "deny", "protocol": "ip",
                          "source": "192.168.10.0/24", "destination": "192.168.20.0/24"},
                         {"action": "permit", "protocol": "ip",
                          "source": "any", "destination": "any"},
                     ],
                     "apply": [{"device": "R1", "interface": "FastEthernet2/0",
                                "direction": "in"}]},
                ],
            },
            ensure_ascii=False,
        ),
    },
]


class NLGenError(RuntimeError):
    pass


# ---------------------------------------------------------------- API key
def load_api_key(explicit: str | None = None) -> str:
    if explicit:
        return _check_key(explicit.strip(), "--key 参数")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return _check_key(key, "环境变量 DEEPSEEK_API_KEY")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "DEEPSEEK_API_KEY":
                return _check_key(v.strip().strip('"').strip("'"), str(ENV_FILE))
    raise NLGenError(
        "找不到 DEEPSEEK_API_KEY。\n"
        f"请在 {ENV_FILE} 里写一行：\n"
        "    DEEPSEEK_API_KEY=sk-你的真实key\n"
        "（文件我已经建好了，把占位符换掉即可）"
    )


def _check_key(key: str, where: str) -> str:
    """挡住「模板没改就开跑」和粘贴时带上多余字符的情况。"""
    if not key:
        raise NLGenError(f"{where} 里的 key 是空的。")
    if "xxxx" in key.lower() or "你的" in key:
        raise NLGenError(
            f"{where} 里还是占位符，不是真实 key。\n"
            "请打开 .env，把 sk-xxxxxxxx… 整段替换成你的真实 key（sk- 开头，32 位左右）。"
        )
    if not key.startswith("sk-"):
        raise NLGenError(f"{where} 里的 key 不是 sk- 开头（len={len(key)}），请检查是否粘错了。")
    if not key.isascii() or any(c.isspace() for c in key):
        raise NLGenError(f"{where} 里的 key 含有空格或非 ASCII 字符，粘贴时可能带进了中文或换行。")
    return key


# ---------------------------------------------------------------- 客户端
@dataclass
class Client:
    key: str
    model: str = MODEL
    url: str = API_URL
    timeout: int = 180
    temperature: float = 0.2
    retries: int = 3

    def chat(self, messages: list[dict]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 8000,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                last = NLGenError(f"HTTP {exc.code}: {detail}")
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise last
            except (urllib.error.URLError, TimeoutError) as exc:
                last = NLGenError(f"网络错误：{exc}")
                time.sleep(2 * (attempt + 1))
        raise last or NLGenError("调用失败")


# ---------------------------------------------------------------- 解析
def _strip_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        return m.group(1)
    # 有些时候会带一句前言，抓第一个 { 到最后一个 }
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return t[i : j + 1]
    return t


def parse_json(text: str) -> dict:
    raw = _strip_fence(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NLGenError(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise NLGenError("模型输出的顶层不是 JSON 对象")
    return data


# ---------------------------------------------------------------- 主流程
@dataclass
class Attempt:
    round: int
    issues: list[str] = field(default_factory=list)
    ok: bool = False
    fixed: int = 0          # 本轮由本地修复器改掉的问题数
    blocked: int = 0        # 本轮被护栏拦下的条数
    elapsed: float = 0.0


@dataclass
class Result:
    topology: Topology | None
    yaml_text: str
    rounds: list[Attempt]
    messages: list[dict]
    guards: list[audit.Guard] = field(default_factory=list)

    @property
    def fixed_total(self) -> int:
        return sum(r.fixed for r in self.rounds)


def translate(
    prompt: str,
    client: Client,
    max_rounds: int = 3,
    verbose: bool = True,
    id_hint: str | None = None,
    use_repair: bool = True,
    log: audit.Log | None = None,
) -> Result:
    """把一句话变成校验通过、且过了护栏的 Topology。

    返回的 topology 为 None 表示彻底失败。

    id_hint 由调用方指定——模型对「编号」几乎不敏感，两次不同的需求很可能都返回
    同一个 id（实测连续两次都是 UC-91），直接用它的 id 会互相覆盖产物。
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    messages += FEWSHOT
    messages.append({"role": "user", "content": prompt})

    if log is not None:
        log.prompt = prompt
        log.model = client.model
        log.add("prompt", text=prompt[:2000])

    rounds: list[Attempt] = []
    last_yaml = ""
    all_guards: list[audit.Guard] = []

    for i in range(1, max_rounds + 1):
        if verbose:
            print(f"[第 {i} 轮] 调用模型…")
        t0 = time.time()
        content = client.chat(messages)
        elapsed = time.time() - t0
        messages.append({"role": "assistant", "content": content})

        if log is not None:
            log.add("raw", round=i, elapsed=round(elapsed, 2), text=content)

        try:
            data = parse_json(content)
        except NLGenError as exc:
            rounds.append(Attempt(i, [str(exc)], elapsed=elapsed))
            if log is not None:
                log.add("round", round=i, errors=1, blocked=0, elapsed=elapsed)
            if verbose:
                print(f"  ✗ {exc}")
            messages.append({"role": "user", "content": f"{exc}。请只输出修正后的完整 JSON 对象。"})
            continue

        data.setdefault("meta", {})
        if id_hint:
            data["meta"]["id"] = id_hint
        elif not data["meta"].get("id"):
            data["meta"]["id"] = "UC-99"

        topo = Topology(data)
        topo.validate()

        # 本地先修：能确定修掉的错不用再问模型
        n_fixed = 0
        if not topo.ok and use_repair:
            rep = repair.repair(data)
            if rep.fixed:
                n_fixed = len(rep.fixed)
                if log is not None:
                    log.add("repair", round=i, fixed=[str(x) for x in rep.fixed])
                if verbose:
                    print(f"  ~ 本地修复 {n_fixed} 处：")
                    for fx in rep.fixed[:5]:
                        print(f"     {fx}")
                data = rep.data
                topo = Topology(data)
                topo.validate()

        last_yaml = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

        errs = [str(x) for x in topo.issues if x.level == "error"]
        if log is not None:
            log.add("validate", round=i, issues=errs)
        if errs:
            rounds.append(Attempt(i, errs, fixed=n_fixed, elapsed=elapsed))
            if log is not None:
                log.add("round", round=i, errors=len(errs), blocked=0, elapsed=elapsed)
            if verbose:
                print(f"  ✗ {len(errs)} 个错误：")
                for e in errs[:8]:
                    print(f"     {e}")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你上一版输出没有通过校验，错误如下：\n"
                        + "\n".join(errs)
                        + "\n\n请逐条修正后，重新输出**完整**的 JSON 对象（不要只给差异，不要解释）。"
                    ),
                }
            )
            continue

        # 护栏：校验通过 ≠ 能交付
        guards = audit.guard(topo, prompt)
        all_guards = guards
        bad = audit.blocked(guards)
        if log is not None:
            log.add("guard", round=i, items=[str(g) for g in guards])
        if bad:
            rounds.append(Attempt(i, [str(g) for g in bad], fixed=n_fixed,
                                  blocked=len(bad), elapsed=elapsed))
            if log is not None:
                log.add("round", round=i, errors=0, blocked=len(bad), elapsed=elapsed)
            if verbose:
                print(f"  ✗ 护栏拦下 {len(bad)} 条：")
                for g in bad:
                    print(f"     {g}")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你上一版输出被安全护栏拒绝：\n"
                        + "\n".join(f"- {g.message}" for g in bad)
                        + "\n\n请修正后重新输出完整的 JSON 对象。"
                    ),
                }
            )
            continue

        rounds.append(Attempt(i, [str(x) for x in topo.issues], True, n_fixed, 0, elapsed))
        if log is not None:
            log.add("round", round=i, errors=0, blocked=0, elapsed=elapsed)
            log.add("done", round=i, fixed=n_fixed, warnings=[str(g) for g in guards])
        if verbose:
            nwarn = len([g for g in guards if g.level == "warn"])
            print(f"  ✓ 校验通过（{len(topo.issues)} 条提示"
                  + (f"，{nwarn} 条护栏提示" if nwarn else "")
                  + (f"，本地修复 {n_fixed} 处" if n_fixed else "")
                  + "）")
        return Result(topo, last_yaml, rounds, messages, guards)

    if log is not None:
        log.add("fail", rounds=len(rounds))
    return Result(None, last_yaml, rounds, messages, all_guards)
