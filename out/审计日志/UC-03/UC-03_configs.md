# UC-03 分公司接入与区域规划

> 需求原文：公司收购了外地分公司，总部 192.168.0.0/24、分公司 192.168.1.0/24，三台路由器，R0 和 R1 之间是骨干链路 10.0.0.0/30 放主干区域，R1 和 R2 之间的 172.16.0.0/30 和扩展链路 172.19.0.0/30 放区域 100，OSPF 进程号 1，要求跨区互通并能看到跨区路由标记。

## 怎么用（先说结论）

**双击 `UC-03.pkt` 打开，配置已经在里面了，通常不需要手工粘贴。**
打开后等 30 秒（STP 收敛 + ARP 学习），就可以按验收报告里挑的用例去 ping。

下面这份配置清单有三个用途：

1. 报告里要附的「分设备配置」
2. 想在**别的拓扑/别的 pkt** 里复现某台设备的配置
3. 万一 pkt 打不开或配置丢了，照这个手工补

---

## 方式一：直接打开 pkt（推荐）

1. 双击 `UC-03.pkt`，Packet Tracer 自动加载
2. 等 **30 秒**（生成树收敛，过早 ping 会丢包）
3. 看连线是否变绿；绿色三角＝链路 up
4. 按 `UC-03_验收报告.md` 里的「预期不通」抽 2 条、「预期通」抽 2 条，在 PC 的
   `Desktop → Command Prompt` 里 ping 验证

## 方式二：命令行整段粘贴（复现/兜底用）

以某台路由器为例：

1. 点开设备 → 切到 **CLI** 标签页
2. 按**回车**唤出提示符 `Router>`，敲 `enable` 进特权模式（`Router#`）
3. 敲 `configure terminal`（`Router(config)#`）
4. 把本文件里该设备的代码块**整段复制**，在 CLI 窗口里**右键 → Paste**
5. 敲 `end` 回到特权模式，再敲 `write memory` 存盘
6. 用 `show running-config` 跟本文件比对

**三个必须注意的点：**

- **PT 是逐行回放，遇到第一行非法命令就中止后面全部**。
  所以粘贴后一定要看一眼有没有 `% Invalid input` —— 出现就说明后面的都没生效。
- 粘贴前确认设备**型号一致**（`Router-PT-Empty` / `2960-24TT` / `PC-PT`）。
  比如 `Router-PT-Empty` 没有 `FastEthernet0/0`，粘 2811 的配置必然第一行就废。
- 粘贴是**追加**不是覆盖。设备里已有旧配置时，先 `erase startup-config` +
  `reload`，或者手工 `no` 掉冲突项。

## 方式三：主机 IP 不是用 CLI 配的

PC / Server 的地址要在图形界面里填，配置清单里的「主机」小节只是给你对照用的数字：

1. 点开 PC → **Desktop** 标签 → **IP Configuration**
2. 选 **Static**
3. 填 IPv4 Address / Subnet Mask / Default Gateway（清单里逐台列好了）
4. 关掉窗口即生效

> 注意：PT 里网关存两份（端口级和引擎级），生成器两处都写了。
> 如果你手工只改了一处，会出现「命令行里网关是对的、但 IP Configuration 窗口是空的」。

## 方式四：VLAN 数据库

交换机上的 `vlan 10` / `name RD` 这类命令**不能只靠命令行粘贴**——
PT 的 VLAN 数据库不在 `running-config` 里，而在设备内部（`flash:vlan.dat`）。
生成器是直接写进 pkt 容器的，所以打开就有。

如果你在**别的交换机**上手工复现，粘贴 `running-config` 后还要单独执行：

```
vlan 10
 name RD
vlan 20
 name FIN
exit
```

然后用 `show vlan brief` 确认 VLAN 真的出现了（端口归属也要对）。

---

## 接线表

| 设备 | 端口 | 对端设备 | 对端端口 | 线型 |
|---|---|---|---|---|
| R0 | Serial0/0 | R1 | Serial0/0 | 串口线 |
| R1 | Serial1/0 | R2 | Serial0/0 | 串口线 |
| R1 | FastEthernet2/0 | R2 | FastEthernet2/0 | 交叉线 |
| PC0 | FastEthernet0 | SW0 | FastEthernet0/1 | 直通线 |
| SW0 | FastEthernet0/24 | R0 | FastEthernet2/0 | 直通线 |
| PC1 | FastEthernet0 | SW1 | FastEthernet0/1 | 直通线 |
| SW1 | FastEthernet0/24 | R2 | FastEthernet3/0 | 直通线 |

## R0（router）

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname R0
!
no ip cef
no ipv6 cef
!
interface FastEthernet2/0
 ip address 192.168.0.254 255.255.255.0
 no shutdown
!
interface Serial0/0
 ip address 10.0.0.1 255.255.255.252
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router ospf 1
 network 192.168.0.0 0.0.0.255 area 0
 network 10.0.0.0 0.0.0.3 area 0
!
ip classless
!
line con 0
!
line vty 0 4
 login
!
end
```

## R1（router）

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname R1
!
no ip cef
no ipv6 cef
!
interface Serial0/0
 ip address 10.0.0.2 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial1/0
 ip address 172.16.0.1 255.255.255.252
 no shutdown
!
interface FastEthernet2/0
 ip address 172.19.0.1 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router ospf 1
 network 10.0.0.0 0.0.0.3 area 0
 network 172.16.0.0 0.0.0.3 area 100
 network 172.19.0.0 0.0.0.3 area 100
!
ip classless
!
line con 0
!
line vty 0 4
 login
!
end
```

## R2（router）

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname R2
!
no ip cef
no ipv6 cef
!
interface FastEthernet3/0
 ip address 192.168.1.254 255.255.255.0
 no shutdown
!
interface Serial0/0
 ip address 172.16.0.2 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface FastEthernet2/0
 ip address 172.19.0.2 255.255.255.252
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
router ospf 1
 network 192.168.1.0 0.0.0.255 area 100
 network 172.16.0.0 0.0.0.3 area 100
 network 172.19.0.0 0.0.0.3 area 100
!
ip classless
!
line con 0
!
line vty 0 4
 login
!
end
```

## SW0（switch）

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname SW0
!
no ip cef
no ipv6 cef
!
interface FastEthernet0/1
 no shutdown
!
interface FastEthernet0/24
 no shutdown
!
end
```

## SW1（switch）

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname SW1
!
no ip cef
no ipv6 cef
!
interface FastEthernet0/1
 no shutdown
!
interface FastEthernet0/24
 no shutdown
!
end
```

## PC0（主机）

- IP：192.168.0.10/24
- 网关：192.168.0.254

## PC1（主机）

- IP：192.168.1.10/24
- 网关：192.168.1.254
