# E 安全护栏：命令白名单 / 危险命令黑名单 / 部署前 CONFIRM

> 对应评分标准 **E（1 分，贯穿全程）**，判定语要求四件齐全，每缺一件扣 0.25：
> ① 命令白名单（正则拦截，白名单外一律拒绝）
> ② 危险命令黑名单（`reload` / `write erase` / `format` 等硬拦截 + 告警）
> ③ 部署前人工 CONFIRM 确认门
> ④ 审计日志（prompt、原始输出、拦截记录、下发内容、验证结果全程落盘）
>
> 实现：`pktlab/cmdguard.py`（①②③）+ `pktlab/audit.py`（④）+ `pktlab/pipeline.py`（把前后串起来）。
> 演示：`python tools/demo_cmdguard.py`　回归：`python tools/test_cmdguard.py`（67 项断言）
> **日志全部真实产出**，不存在手写的审计记录。

---

## 0 一句话说清「命令级」和「语义级」的区别

| | 判断对象 | 例子 | 归属 |
|---|---|---|---|
| **语义护栏** | 配置合不合理 | `access-list 100 permit ip any any` —— 语法全对，防护等于零 | `audit.py`（此前已做） |
| **命令护栏** | 这行命令允不允许出现 | `reload` —— 语义上毫无问题，但绝不该出现在生成结果里 | `cmdguard.py`（本次补） |

两者不能互相顶替。评分标准 E 要的是**命令级**，所以单独一层。

---

## 1 执行顺序（护栏能叫护栏的前提）

```
DSL 校验 → 语义护栏 → 生成配置行 → ① 命令级筛查 → ② 人工 CONFIRM → 装配写 .pkt
```

关键点：**筛查和确认都在 `assemble.build()` 之前**。
任何一步不通过就抛异常，磁盘上不会出现新的 `.pkt`。
（`tools/test_cmdguard.py` 的第 ④ 组测试专门验证这一点：拦截后目录是空的。）

```python
# pktlab/pipeline.py
configs = config.generate_all(topo)     # ① 先算配置行
gate = cmdguard.check_all(configs)      # ② 命令级筛查
if not gate.passed:
    raise cmdguard.CmdBlocked(gate)     #    → 不落盘
source = confirmer(gate, configs)       # ③ 人工确认（默认交互）
result = assemble.build(topo, ...)      # ④ 才装配
```

---

## 2 ① 命令白名单（default-deny，43 条）

每行先归一化（去首尾空白、压空格），再按**顺序**判定：黑名单 → 白名单 → 拒绝。
一条都不命中的按 `CMD_UNLISTED` 拒绝，不存在「没写规则就放过去」。

覆盖本项目会产出的全部命令形态：

| 类别 | 规则（正则概览） |
|---|---|
| 全局 | `version N(.N)`、`hostname X`、`no service timestamps ...`、`no ip cef`、`ip classless` |
| 接口 | `interface (FastEthernet\|GigabitEthernet\|Serial\|Loopback)N/N(.N)`、`ip address A M`、`no shutdown`、`clock rate N`、`duplex/speed` |
| 交换 | `switchport mode (access\|trunk)`、`switchport access vlan N`、`switchport trunk ...`、`vlan N`、`name X` |
| 子接口 | `encapsulation dot1Q N` |
| 路由 | `router (rip\|ospf N\|bgp N)`、`network A [M] [area N]`、`no auto-summary`、`area N stub`、`passive-interface X`、`neighbor A remote-as N` |
| 静态 | `ip route A M NH [AD]`、`ip default-gateway A` |
| ACL | `access-list N (permit\|deny) ...`、`ip access-group N (in\|out)` |
| 行配置 | `line (con\|vty\|aux) N [N]`、`login`、`exit`、`end`、`!` |

**为什么这么严**：DSL 目前不支持「自定义命令」字段，所以出现白名单外的命令，
要么是模型幻觉（编了 `ip domain-name`、`banner motd`、`ip nat`），
要么是提示注入（有人让模型往配置里塞东西）。两种情况都该拒绝。

想扩白名单必须改代码，改完要过 `tools/test_cmdguard.py` —— 不是改个配置文件就能放开。

---

## 3 ② 危险命令黑名单（12 条，硬拦截 + 告警）

| 代码 | 匹配 | 为什么必须拦 |
|---|---|---|
| `CMD_RELOAD` | `reload`、`reload in 5` | 重启设备，中断全网、丢未保存配置 |
| `CMD_ERASE` | `write erase`、`erase startup-config`、`erase nvram:` | 擦掉启动配置，重启后配置全丢 |
| `CMD_FORMAT` | `format` | 格式化 flash，系统文件被删 |
| `CMD_DELETE` | `delete`、`delete /force` | 删 flash 文件，可能导致 IOS 缺失 |
| `CMD_NO_ROUTING` | `no ip routing` | 路由器退化成主机，全网跨网段立刻断 |
| `CMD_BOOT` | `boot system`、`config-register`、`no boot` | 改启动参数，设备可能起不来 |
| `CMD_SETUP` | `setup` | 打断当前会话 |
| `CMD_HTTP` | `ip http server` | 扩大管理面攻击面 |
| `CMD_SNMP` | `snmp-server community public/private` | 默认团体名等同无认证 |
| `CMD_WEAK_ENABLE` | `enable password/secret` 空口令或 `cisco`/`123`/`admin` | 弱口令 |
| `CMD_DEBUG_ALL` | `debug all` | CPU 打满 |
| `CMD_CRYPTO` | `crypto key generate` | 需交互，回放时会卡死 |

判定优先级：**黑名单先查**。命中的即使同时符合白名单也拦（例如将来若把 `ip http server`
写进白名单，黑名单仍然生效）。

大小写、多余空格都能识别（`RELOAD` / `  reload  ` / `No IP Routing` 均被拦），
这一点有测试覆盖 —— 模型很爱返回大小写混乱的命令。

---

## 4 ③ 部署前 CONFIRM 确认门

交互流程（`cmdguard.interactive_confirm`）：

```
部署前确认（命令级护栏结果）
- 设备 7 台，配置合计 156 行
- 白名单：43 条规则，未列入的命令一律拒绝
- 黑名单：12 条危险命令规则
- 拦截 0 条，告警 0 条

确认无误请输入大写 CONFIRM（其他任意输入即中止）：
```

三个设计决定：

1. **必须手敲大写 `CONFIRM`**，直接回车、输入 `y`、`yes` 都不算。
2. **没有 `--yes`、没有环境变量、没有 `--force`**。评分标准红线第 3 条是「绕过或关闭自设的
   安全护栏进行演示」，所以刻意不提供旁路 —— 有旁路就等于没有这条护栏。
3. **非交互场景必须显式传 `--confirm CONFIRM`**，且审计日志把它记成
   「命令行参数 --confirm」，**不会伪装成人工确认**。
   在非 tty 环境下 `interactive_confirm` 直接拒绝（`ConfirmRefused`），不会静默放行。

| 入口 | 确认方式 |
|---|---|
| `python gen.py x.yaml`（终端） | 交互敲 CONFIRM |
| `python nlgen.py "…"`（终端） | 交互敲 CONFIRM |
| `start.cmd` 菜单 [1]/[3] | 交互敲 CONFIRM |
| `start.cmd` 菜单 [2]（批量刷样例） | 一次性确认后，来源记为「交互菜单确认（一次性批准 N 个）」 |
| 脚本 / 批量 | `--confirm CONFIRM`，来源如实记录 |

---

## 5 ④ 审计日志（E④ 的两个新增小节）

`out/{uid}_nlog.md` 现在按下面顺序落盘，一份文件能从「用户说了什么」读到「pkt 里写了什么」：

| 小节 | 内容 | 来源事件 |
|---|---|---|
| 需求原文 / 模型 / 轮数 / 耗时 / 修复数 | 本次生成的元信息 | `meta`、`round` |
| 语义护栏裁决 | ACL permit-any-first 之类的判断 | `guard` |
| **命令级筛查** | 配置行数、拦截/告警条数、命中明细 | `cmdscreen`（新增） |
| **部署确认** | 确认来源（人工/命令行/菜单批量）、确认对象 | `confirm`（新增） |
| 自动修复 | 每轮修了哪几处 | `repair` |
| 校验问题 | 每轮的错误清单 | `validate` |
| **下发内容** | 每台设备逐行配置（可折叠） | `deliver` |
| **验证结果** | 推算通/断言/一致性 + 口径声明 | `verify` |

JSONL 版本（`{uid}_audit.jsonl`）是同一批事件的机器可读格式，一行一个事件。
两种格式都由 `audit.Log` 生成，**没有一份是手写的**。

---

## 6 演示证据（可现场复现）

`python tools/demo_cmdguard.py` 的真实输出（节选）：

```
场景 2：配置里混进危险命令（模拟模型幻觉 / 提示注入）
------------------------------------------------------------------------
结果：拦截，未生成任何 .pkt
  [拦截/CMD_RELOAD] R1 第 40 行 `reload in 5` —— 重启设备，会中断全网并丢掉未保存配置
  [拦截/CMD_ERASE] R2 第 40 行 `write erase` —— 擦除启动配置，设备重启后配置全丢
  [拦截/CMD_NO_ROUTING] R3 第 42 行 `no ip routing` —— 关闭 IP 路由，路由器立刻退化成普通主机
  目录内容：（空）

场景 3：白名单外的命令（default-deny）
  `ip domain-name corp.example`                  → CMD_UNLISTED (block)
  `username ops privilege 15 secret 5 $1$xyz`    → CMD_UNLISTED (block)
  `ip nat inside source list 1 ... overload`     → CMD_UNLISTED (block)
  `banner motd #Unauthorized access prohibited#` → CMD_UNLISTED (block)
```

注意场景 2 的最后一行：**目录是空的**。这就是「拦截」和「警告」的区别。

---

## 7 回归（`tools/test_cmdguard.py`，67 项断言）

| 组 | 验什么 | 结果 |
|---|---|---|
| ① 黑名单 | 14 条危险命令全部命中正确错误码；大小写/空格变体也能识别 | 18/18 |
| ② 白名单 | 5 条幻觉命令被拒；27 条合法命令放行 | 32/32 |
| ③ 正常配置 | `examples/uc01~uc07` 共 5 个用例、707 行配置 **0 拦截 0 告警** | 6/6 |
| ④ 不落盘 | 拦截后无 pkt；未确认后无 pkt；确认后才产出 | 6/6 |
| ⑤ 审计如实 | `cmdscreen` / `confirm` 事件存在，确认来源写明「自动化」 | 5/5 |

第 ③ 组是防「护栏过度」的：如果把正常流程也拦死，这套东西就没法用了。

---

## 8 能力边界（报告里要写明白的）

- 白名单是**正则层面**的判断，不是 IOS 语法解析器。`access-list` 后面的参数只校验形态，
  不校验语义（语义由 `dsl.py` 的 ACL 校验码负责）。
- 确认门保护的是「生成 → 落盘」这一段。用户自己打开 PT 手工敲 `reload` 不在这套护栏范围内
  —— 本项目只生成本地 `.pkt` 文件，不对任何真实设备下发。
- 黑名单是**已知集合**，不是完备集合。新增危险命令要改 `BLACKLIST` 并补测试。
