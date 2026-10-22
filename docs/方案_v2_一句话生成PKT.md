# 方案 v2：一句话生成拓扑图 + 可直接提交的 .pkt

> 目标重定义：`自然语言需求` → ①拓扑图（SVG/PNG）②完整 `.pkt`（双击可在 Packet Tracer 打开、设备已连线、配置已下发）
> 约束：无虚拟化环境（无 VMware/VirtualBox/Docker/GNS3/CML）、本地模型已删除、**剩余 2 周**

---

## 0. 可行性判定（已验证，不是推测）

| 验证项 | 结果 | 证据 |
|---|---|---|
| `.pkt` 是否为可逆容器 | **是**，四层封装全部可纯 Python 还原 | 文档 + 开源实现（pka2xml / pka-decipher / pktfile / packet-tracer-mcp） |
| 能否解码真实 PT 文件 | **是**，3 个真实实验文件全部解出 870KB–988KB XML | `spike/seeds/*.xml` |
| 能否编码回真实 PT 文件 | **是，且字节完全一致** | 3/3 文件 `encrypt(decrypt(f)) == f`，长度逐字节相同 |
| 设备配置是否在文件内 | **是**，每设备带 `<RUNNINGCONFIG><LINE>…</LINE></RUNNINGCONFIG>` | 见 `spike/seeds/tutorial10.xml` |
| 连线是否可编程 | **是**，`<LINK>` 引用 `save-ref-id` + 端口名 + 坐标 | 同上 |
| 是否需要 PT 才能生成 | **不需要**（只需 PT 来"验收"打开效果） | — |

**结论：这条路走得通，而且是本次作业里唯一能真正做到"一句话出成品"的路径。**
最大的未知只剩一个：**PT 是否接受被我们改过 XML 的文件**（会不会像社区记录的那样静默打开空白画布）。这必须在 D2 用真 PT 验证，是全项目的 go/no-go 闸门。

---

## 1. 技术原理

### 1.1 容器（PT 6.0+，已验证）
```
XML(UTF-8)
 → Qt qCompress：4 字节大端原文长度 + zlib
 → 位置混淆：b[i] ^= (len - i) & 0xFF
 → Twofish-EAX：key = 0x89×16，nonce = 0x10×16，尾部追加 16 字节 tag
 → 反转 + 位置混淆：out[n-1-i] = in[i] ^ ((n - i*n) & 0xFF)
```
解码为逆序。**已在本机跑通**，代码在 `spike/ptlab/`（codec.py 158 行 + twofish.py 278 行，纯标准库，零第三方依赖）。

### 1.2 内部 XML 结构（真实文件实测）
```
<PACKETTRACER5>
  <VERSION>8.2.2.0400</VERSION>
  <NETWORK>
    <DEVICES>
      <DEVICE>
        <ENGINE><TYPE model="Router-PT-Empty">Router</TYPE><NAME>Router0</NAME><POWER>true</POWER>
          <MODULE>…eNonRemovableModule / ePtRouterModule / PORT 树…</MODULE>
        </ENGINE>
        <SAVE_REF_ID>…</SAVE_REF_ID>
        <RUNNINGCONFIG><LINE>hostname R0</LINE>…</RUNNINGCONFIG>   ← 配置注入点
        <WORKSPACE><LOGICAL><X/><Y/><MEM_ADDR/><DEV_ADDR/></LOGICAL></WORKSPACE>  ← 坐标注入点
      </DEVICE>
    </DEVICES>
    <LINKS><LINK><CABLE><FROM>save-ref-id:…</FROM><PORT>Fa0/1</PORT><TO>…</TO></CABLE></LINK></LINKS>
    <WORKSPACE><LOGICAL>…</LOGICAL></WORKSPACE>
    <OPTIONS>…</OPTIONS>
    <PHYSICALWORKSPACE>…</PHYSICALWORKSPACE>
    <SCENARIOSET>…</SCENARIOSET>
  </NETWORK>
</PACKETTRACER5>
```

### 1.3 三个安全的注入点
1. **`<RUNNINGCONFIG>`** —— 把生成的整段 IOS 配置按行包成 `<LINE>`，替换即可（最高价值，风险最低）
2. **`<NAME>` / PC 的 `IP`/`SUBNET`/`DEFAULT_GATEWAY`** —— 设备命名与主机编址
3. **`<X>`/`<Y>`** —— 画布坐标，决定拓扑图与 PT 里看到的布局是否一致

高风险的注入是**增删设备**（要克隆整棵 MODULE 树 + 重新分配 `SAVE_REF_ID`/`MEM_ADDR` 并同步 LINK 引用）。这是第二周才碰的东西。

---

## 2. 系统架构（五层，全部本地 Python + 一个 LLM API）

```
① 意图层   NL ──(LLM, JSON Schema 强约束)──▶ topology.yaml (DSL)
② 校验层   ipaddress 集合运算：地址重叠 / 网关错配 / 掩码非法 / VLAN 引用悬空 / 网段容量不足
③ 渲染层   DSL ──▶ 拓扑图(SVG+PNG)  +  IOS 配置(Jinja2 模板，确定性)
④ 打包层   DSL + 配置 ──(骨架选型 + XML 注入 + 四层编码)──▶ output/UC-xx.pkt
⑤ 闭环层   解码生成的 pkt ──▶ 配置级仿真(ping矩阵/路由断言) ──▶ 结构化验收报告
           故障注入(改 XML) ──▶ Agent 读回 ──▶ diff 定位 ──▶ 修复 ──▶ 重编码（≤3 轮，日志全留）
```
关键设计：**LLM 只负责第 ① 层**。配置由确定性模板渲染，`pkt` 由确定性代码拼装。LLM 的不可靠性被隔离在最上游，且被 JSON Schema 挡住。

---

## 3. 三种 .pkt 生成策略

| 策略 | 做法 | 风险 | 覆盖 |
|---|---|---|---|
| **S1 骨架变体**（打底） | 预置 4–5 个骨架 pkt（如 3R+2PC / 2R+2PC / 1R+2SW+6PC / 2R+1SW+2PC / 1R+1SW+2PC），只替换名称、配置、坐标 | **低**（结构不动） | UC-01/02/03/04/06/07/08/09/10 |
| **S2 设备克隆**（冲刺） | 从"设备大全"pkt 里克隆 `<DEVICE>` 节点 + 重建 `<LINK>`，任意组合设备数量 | **中高**（SAVE_REF_ID / MEM_ADDR / 物理工作区映射） | 任意拓扑，含 UC-05 六台 PC、UC-11/12 |
| **S3 固定模板**（兜底） | 每个 UC 一个手搭 pkt，只换配置 | 极低 | 全部，但工作量随 UC 线性增长 |

执行顺序：**S1 先跑通 8 个 UC → S2 攻克增删设备 → S3 兜住剩下的**。S2 失败不影响交付。

---

## 4. 两周排期（按每天 3–4 小时）

| Day | 任务 | 产出 / 判定 |
|---|---|---|
| D1 | 装 PT 8.2.2；申请 API key；工程骨架；把 3 个种子文件解码跑通 | 环境就绪 |
| **D2** | **P0 闸门**：改一个设备名 + 一段配置 → 重编码 → PT 打开 | **三种结果：①正常打开且配置生效（全速前进）②打开但配置未生效（转"骨架+粘贴"）③空白画布（退回 S3）** |
| D3–4 | DSL + JSON Schema + 冲突检测器 + 拓扑图渲染 | A 维可自测 |
| D5–6 | 配置生成器（RIP v1/v2、OSPF、静态、NAT、ACL、VLAN、BGP、IPv6 隧道）+ 四类静态校验 | B 维可自测 |
| D7–8 | pkt 打包器（S1）+ 12 个 UC 批量生成 | 成品率统计 |
| D9 | 配置级仿真验收（ping 矩阵推演 / 路由表断言 / 输出报告） | C2 主体 |
| D10 | PT 内真实 ping（pyautogui 定点操作 + 剪贴板取输出） | C2 演示证据 |
| D11 | 故障注入 + diff 自动修复闭环 | C3 满分形态 |
| D12 | 四件套护栏 + 审计日志 + 定量评估表（12 样例） | E 满分 + D1 |
| D13 | 报告 ≤6 页 + ≥2 个失败案例 | D2/D3 |
| D14 | 演示排练 + 打包（`学号_姓名_拓展作业 LLM.zip`） | 提交 |

**D2 是分水岭**：闸门不过就地降级，不浪费后面 12 天。

---

## 5. 模型选型（只能用 API）

| 角色 | 选择 | 理由 |
|---|---|---|
| 主模型 | **DeepSeek-V3.x**（或 qwen-plus） | 128K 上下文、JSON Schema/function calling 稳、中文强、全项目成本约 ¥5–30 |
| 备用/对照 | 智谱 GLM 系或通义免费额度 | 用于 D2「失败案例分析」的第二组样本，同时防单点故障 |
| 不用 | 任何本地模型 | 已删除；16G 显存跑 30B 的上下文与稳定性都不够 |

工程约束：
- 一律 `response_format=json_schema`，服务端强约束输出，杜绝"格式漂移"类失败（这本身也是 D2 的工程对策素材）
- 两级重试：schema 校验失败 → 带错误信息回灌重试 1 次 → 仍失败则降级到规则引擎兜底
- 所有 prompt / 原始响应 / 重试记录写审计日志（E④ 要求）

---

## 6. 得分映射

| 维度 | 本方案落点 | 预期 |
|---|---|---|
| A（4） | DSL + JSON Schema + 四类冲突检测（UC-12 陷阱自动拒绝+反问）+ 3 样例 | 4 |
| B（5） | 实验 4-10/4-11 配置直接可跑；四类静态检查 + 拦截录屏；B3 需 JDK（Batfish allinone jar），可选 | 4–5 |
| C（6） | C1 直接产出 .pkt（远超"粘贴指南"口径）；C3 文件层 diff 修复闭环，天然 ≤3 轮且日志可复现；C2 双轨（仿真推演 + PT 内真实 ping） | 5–6 |
| D（4） | 12 个 UC 即 12 个样例；三项指标自动统计；双模型对照出失败案例；现场演示 | 3.5–4 |
| E（1） | 白名单 / 黑名单 / CONFIRM / 审计日志四件套 | 1 |
| **合计** | | **17.5–20（卓越区）** |

红线自检：全部操作仅针对本地 `.pkt` 文件与自建 DSL，不触达任何真实设备/校园网；故障注入只做"错掩码 / 漏 network"，不做扫描、欺骗、泛洪。

---

## 7. 风险与降级路径

| 风险 | 概率 | 降级方案 |
|---|---|---|
| PT 打不开被改过的 XML | 中 | 退 S3：每个 UC 手搭骨架，只注入配置；再不行退"配置手册 + 一键粘贴"（C1 仍满分口径） |
| `<RUNNINGCONFIG>` 不自动生效 | 中 | 生成 pkt 骨架 + 逐台粘贴（人工 2 分钟），C1 记 1 分，其余不受影响 |
| 设备克隆导致文件损坏 | 中 | 只用 S1 固定骨架，减少 UC 覆盖数（保 8 个即可满足 D1 的 ≥10 样例？需 S2 或增加 S3 手搭） |
| pyautogui 点不准 | 中 | 放弃 PT 内真实 ping，只用配置级仿真 + 手工录一次屏作为演示证据 |
| 时间不够 | 中 | 优先序：E → A → B → S1 打包 → D → C2/C3；砍掉 B3、UC-11/12 |

---

## 8. 需要确认的三件事

1. **Packet Tracer 能不能装？** 本机没有（全盘无 `PacketTracer.exe`、无 `.pkt`）。装 PT 8.2.2 是硬前提——不光为验证，D3 现场演示也要用。有没有 NetAcad 账号 / 安装包？
2. **API key 有没有？** DeepSeek / 通义 / 智谱任一个即可；没有的话我给申请步骤（含免费额度方案）。
3. **12 个 UC 要全覆盖，还是保 8 个？** D1 只要求 ≥10 个样例，课内实验 ≥5 个即可。

---

## 附：当前已落地的资产

```
D:/bouns/spike/
  ptlab/codec.py      pkt 四层编解码（已验证：3 个真实文件字节级往返一致）
  ptlab/twofish.py    纯 Python Twofish（MIT）
  seeds/*.pkt         3 个真实实验种子（RIP / OSPF / 单臂路由）
  seeds/*.xml         对应的明文 XML，可直接研究结构
```
