# LLM Agent 驱动的网络拓扑自动生成与安全部署

把一句话需求变成**能直接双击打开的 Packet Tracer 拓扑文件**（`.pkt`），
交付前逐项推算连通性、路径与路由表，并在命令下发前过一道护栏。

```cmd
start.cmd                          :: 一次性装环境 + 自检
run uc01                           :: 用 examples/uc01.yaml 生成 pkt 与报告
run-nl "三台路由器两个 PC，左边 192.168.16.0/25 ..."   :: 走自然语言通道
run-accept UC-01                   :: 只跑验收报告
selftest.cmd                       :: 七套自测
python tools\diag_cfg.py bad_configs\case1_漏network   :: 给一份坏配置，定位并修
```

产物落在 `out/`：`{UID}.pkt`、`{UID}_topology.svg`、`{UID}_configs.md`
（分设备配置 + 粘贴指南）、**`{UID}_编址规划.md`**（分层规划图 + IP/VLAN 编址表 + 设备策略表）、
`{UID}_验收报告.md`、`{UID}_audit.jsonl`。

## 为什么不是"调个 API 出 YAML"就完事

Packet Tracer 没有可脚本化的仿真接口，所以这个项目真正的难点不在生成，而在**三处确定性**：

| 问题 | 做法 |
|---|---|
| 怎么产出真 `.pkt`（不是让用户自己画） | 逆向容器格式：XML → qCompress → 逐字节异或 → Twofish-EAX，纯 Python 实现，见 `pktlab/codec.py` |
| 怎么知道生成的东西**能通** | 静态推算：同网段二层直通；跨网段找路由器路径并要求沿途都有路由；ACL 按序首条命中；再翻译成 `tracert` 跳序与 `show ip route` 断言 |
| 怎么防止模型写出毁设备的命令 | 43 条白名单（不认识的默认拒绝）+ 12 条黑名单 + 无旁路的确认门，见 `pktlab/cmdguard.py` |

## 目录

```
pktlab/          核心库
  codec.py       .pkt 容器编解码（qCompress + Twofish-EAX）
  dsl.py         拓扑 DSL、语义校验
  pkt.py         骨架设备读写（含端口地址收敛）
  assemble.py    克隆 / 连线 / 布局
  config.py      生成 IOS 配置（IP/VLAN/RIP/OSPF/ACL/静态）
  cmdguard.py    命令级白名单、黑名单、确认门
  accept.py      静态验收（ping 矩阵 / traceroute / 路由表 / 等价多路径）
  faults.py      24 类故障注入（DSL 层）
  repair.py      自动定位与修复闭环（DSL 层）
  diag.py        配置级排障：读 running-config → 定位 → 改那几行 → 复检
  plan.py        编址规划书：分层规划图 + IP/VLAN 表（给人看的那一份）
  clone.py       设备块克隆：重分配全套身份标识（PT 载入索引靠它们）
  layout.py      分层布局（按 layer 字段分层，宽层按 VLAN 边界拆行）
  nl.py          自然语言 → DSL（DeepSeek）
  pipeline.py    一条龙流水线
examples/        用例（课内实验 + 实验 4-10 / 4-11 / 8-1 三层园区网）
seeds/           三套骨架 pkt
tools/           批处理脚本与八套自测
  verify_pkt.py  pkt 静态一致性校验器（PT 打不开问题的根治）
docs/            评分对照、课内实验对齐、自动化验收、PT 验证清单、pkt 一致性校验
out/             生成产物（pkt / 报告 / 配置 / 审计日志）
```

## 实验 4-10 / 4-11 的对照结论

三个用例共用同一套拓扑与编址（`192.168.16.0/25` 与 `192.168.16.128/26`
同属主网络 `192.168.16.0/24`），只有路由协议不同。以下均为 **Packet Tracer 实测值**：

| | UC-80 RIPv1 | UC-81 RIPv2 | UC-82 OSPF |
|---|---|---|---|
| 自动汇总 | `is in effect` | `is not in effect` | — |
| PC0 → PC1 | **不通**（100% 丢包，网关回不可达） | 通 | 通 |
| `tracert` | 断在第 1 跳 | **3 跳**，第 2 跳 `30.0.0.2`（串口） | **4 跳**，第 2 跳 `10.0.0.2`（绕行） |
| R0 去 `/26` | 无路由 | `[120/1]` 走串口 | `[110/3]` 走 `Fa3/0` |
| 等价负载均衡 | — | — | R2 去 `30.0.0.0/30` 两条 `[110/65]` |

v1 不通不是配错，而是有类协议的固有行为：通告不带掩码，两台边缘路由器都认为
整个 `/24` 在自家门口，于是丢弃对方的路由。

## 能力边界（不做的部分也写清楚）

- 只建模 IPv4 ICMP；不带 port 的 ACL 参与判定，带 port 的跳过
- 不建模收敛时间、ARP、STP 阻塞、NAT、策略路由、浮动静态的 AD 选路
- 等价多路径只给"代价相同、有多条候选"的判据，不做流量分摊推算
- 认证用的是 `Router-PT-Empty` 骨架，接口名与手册的 2911 不同（映射见 `docs/课内实验对齐.md`）

## 环境

Windows + Python 3.13，依赖见 `requirements.txt`。自然语言通道需要 `DEEPSEEK_API_KEY`
（复制 `.env.example` 为 `.env` 填入）；只用 YAML 通道不需要任何 key，也不联网。

Packet Tracer 仅用于人工核对，生成与验收全程不依赖它。
