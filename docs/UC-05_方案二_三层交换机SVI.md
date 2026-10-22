# UC-05 方案二：三层交换机 SVI（不使用路由器）

> 附录 UC-05 要求「分别用**两种方案**实现跨 VLAN 单播互通」：
> 方案一 路由器单臂（已生成 `out/UC-05.pkt`，见 `out/UC-05_configs.md`）；
> 方案二 三层交换机 SVI（本文）。
>
> **为什么方案二没有 pkt**：`.pkt` 装配器是基于种子文件「只删不增」的 —— 骨架里只有
> `Router-PT-Empty` / `2960-24TT` / `PC-PT` 三种设备槽位，**没有 3560 这类三层交换机**。
> 新增设备型号需要克隆整棵 MODULE 树并重建 `SAVE_REF_ID` / `MEM_ADDR` 与链路引用，
> 属于已知限制（`docs/方案_v2_一句话生成PKT.md` 第 6 节）。
> 所以方案二以**完整可粘贴的配置 + 手工搭建步骤**交付，并在 PT 里按第 4 节验证。

---

## 1 拓扑与编址（与方案一完全一致，只换三层设备）

```
PC0 ─┐
PC1 ─┼─ Fa0/1..3 ─┐
PC2 ─┘            ├─ L3SW（3560-24PS）
PC3 ─┐            │   VLAN 10 = 192.168.1.0/24  网关 192.168.1.254（SVI Vlan10）
PC4 ─┼─ Fa0/4..6 ─┘   VLAN 20 = 192.168.2.0/24  网关 192.168.2.254（SVI Vlan20）
PC5 ─┘
```

| 设备 | 端口 | 归属 | 地址 |
|---|---|---|---|
| PC0 / PC1 / PC2 | Fa0 | access VLAN 10 | 192.168.1.11 / .12 / .13，网关 .254 |
| PC3 / PC4 / PC5 | Fa0 | access VLAN 20 | 192.168.2.11 / .12 / .13，网关 .254 |
| L3SW | Fa0/1–Fa0/3 | access VLAN 10 | — |
| L3SW | Fa0/4–Fa0/6 | access VLAN 20 | — |
| L3SW | Vlan10 | SVI | 192.168.1.254/24 |
| L3SW | Vlan20 | SVI | 192.168.2.254/24 |

和方案一的区别只有一件事：**网关从路由器的 dot1Q 子接口搬到了交换机的 SVI 上**，
交换机内部靠 `ip routing` 做 VLAN 间转发，不需要外部路由器，也不需要 trunk 上联。

---

## 2 完整配置（直接在 3560 的 CLI 里整段粘贴）

```
enable
configure terminal
hostname L3SW
!
vlan 10
 name FIN
vlan 20
 name MKT
exit
!
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 10
!
interface FastEthernet0/2
 switchport mode access
 switchport access vlan 10
!
interface FastEthernet0/3
 switchport mode access
 switchport access vlan 10
!
interface FastEthernet0/4
 switchport mode access
 switchport access vlan 20
!
interface FastEthernet0/5
 switchport mode access
 switchport access vlan 20
!
interface FastEthernet0/6
 switchport mode access
 switchport access vlan 20
!
interface Vlan10
 ip address 192.168.1.254 255.255.255.0
 no shutdown
!
interface Vlan20
 ip address 192.168.2.254 255.255.255.0
 no shutdown
!
ip routing
end
write memory
```

三个容易漏的点：

1. **`ip routing` 必须开**。3560 默认是开的，但 2960 不支持三层，别拿 2960 试。
   注意这一行不在本项目命令白名单里（生成器不产出它），手工搭的时候要自己敲。
2. **SVI 的 `no shutdown`**。VLAN 接口默认是 administratively down，
   而且只有当该 VLAN 里至少有一个 up 的 access 口时 SVI 才会真正 up。
3. **VLAN 要先在 VLAN 数据库里建**（`vlan 10` / `vlan 20`），
   只写 `interface Vlan10` 不建 VLAN，SVI 起不来。

---

## 3 与方案一的对比

| | 方案一 单臂路由 | 方案二 三层交换机 SVI |
|---|---|---|
| 三层设备 | 路由器 `Router-PT-Empty` | 三层交换机 `3560-24PS` |
| 网关载体 | `Fa2/0.10` / `Fa2/0.20` 子接口（`encapsulation dot1Q`） | `interface Vlan10` / `Vlan20` |
| 交换机上联 | 需要一个 **trunk** 口接路由器 | **不需要**（网关就在交换机里） |
| 跨 VLAN 流量路径 | PC → SW → 路由器 → SW → PC（一根线上跑两趟） | PC → SW 内部转发 → PC |
| 瓶颈 | 上联单链路带宽 | 交换机背板 |
| PT 里的设备成本 | 1 路由器 + 1 交换机 | 1 台三层交换机 |
| 本项目能否生成 pkt | ✅ `out/UC-05.pkt` | ❌ 骨架无三层交换机槽位 |

结论（报告里可以这么写）：**两种方案在 PT 里都能通，方案二路径更短、没有单臂瓶颈，
但设备成本高；方案一设备便宜、配置直观，代价是跨 VLAN 流量二次经过上联链路。**

---

## 4 在 PT 里怎么搭 + 怎么验证

### 搭建

1. 放 1 台 `3560-24PS`、6 台 `PC-PT`，PC 用直通线接交换机的 Fa0/1–Fa0/6
2. 按第 2 节把配置整段粘进 3560 的 CLI（`enable` → `configure terminal` → 粘贴 → `end`）
3. 6 台 PC 的 IP Configuration 图形界面里填：地址 / 掩码 255.255.255.0 / 网关 .254

### 验证（对应附录「两种方案分别验证 6 台 PC 两两互通」）

| 步骤 | 命令 | 预期 |
|---|---|---|
| ① VLAN 是否建起来 | `show vlan brief` | 出现 VLAN 10（FIN）、VLAN 20（MKT），Fa0/1–3 在 10、Fa0/4–6 在 20 |
| ② SVI 是否 up | `show ip interface brief` | `Vlan10 192.168.1.254 YES manual up up`，`Vlan20 192.168.2.254 YES manual up up` |
| ③ 三层转发是否开 | `show ip route` | 有 `C 192.168.1.0/24 is directly connected, Vlan10` 和 `C 192.168.2.0/24 ..., Vlan20` |
| ④ 同 VLAN 互通 | PC0 `ping 192.168.1.12`、`ping 192.168.1.13` | 4/4 通 |
| ⑤ 跨 VLAN 互通 | PC0 `ping 192.168.2.11`、`ping 192.168.2.12`、`ping 192.168.2.13` | 4/4 通，TTL **127**（一跳路由） |
| ⑥ 对向下行 | PC3 `ping 192.168.1.11` | 4/4 通，TTL 127 |
| ⑦ 全部 15 对 | 6 台 PC 两两 ping（15 对 × 2 方向） | 全通 |

TTL 的判据：PC 发出 128，经过一次三层转发变 127 —— **127 就说明确实走了 SVI，
而不是「同网段误判」**。方案一（单臂）跨 VLAN 也是 127；如果看到 126 说明多绕了一跳，
通常是 PC 的网关填错了、被别的路由器接走了。

---

## 5 方案一的验收数据（自动化部分）

方案一是生成出来的，所以有完整验收报告（`out/UC-05_验收报告.md`）：

| 指标 | 数值 |
|---|---|
| 主机对 | 30 组（6 台 PC 两两） |
| 推算通 / 不通 | 30 / 0 |
| 断言 | 12 / 12 通过（同 VLAN 3 对 + 跨 VLAN 9 对 ×2 方向抽样） |
| traceroute 路径断言 | 30 条（同 VLAN 1 跳、跨 VLAN 2 跳，首跳 `192.168.1.254` / `192.168.2.254`） |
| 路由表断言 | 2 条（R1 上两个 `C` 直连，因为网关是子接口） |
| 一致性检查 | 13 / 13 |

方案二因为是手工搭建，不在自动化验收范围内 —— 报告里按第 4 节的 7 步给 PT 截图即可。
**这一点在报告里如实写，不把手工验证算成自动化结果。**
