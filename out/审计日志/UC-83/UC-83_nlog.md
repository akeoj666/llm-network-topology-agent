# 生成审计日志

- 需求原文：构建采用三层网络架构的小型园区网：Internet 服务器 → ISP 路由器 → 园区路由器（NAT+ACL）→ 核心层 → 汇聚层 → 接入层，四个部门各一个 VLAN，信息中心独立 VLAN，PC 用 DHCP 获取地址。

- 模型：（YAML 通道，无模型参与）
- 通道：YAML 直读
- 结果：通过

## 护栏裁决

- [提示/NAME_STYLE] Core 是 router，建议用 R/RT 开头，方便读图和写报告
- [提示/NAME_STYLE] Agg1 是 router，建议用 R/RT 开头，方便读图和写报告
- [提示/NAME_STYLE] Agg2 是 router，建议用 R/RT 开头，方便读图和写报告

## 命令级筛查

| 指标 | 数值 |
|---|---|
| 配置行 | 433 行，21 台设备 |
| 拦截 / 告警 | 0 / 0 |

> 全部命令都在白名单内，未命中黑名单。

## 部署确认

- 确认来源：**命令行参数 --confirm**
- 确认对象：21 台设备 / 433 行配置

## 下发内容

- 产物：`UC-83.pkt`
- 设备 21 台，合计 433 行配置

| 设备 | 配置行数 |
|---|---|
| Router0 | 36 |
| Router1 | 37 |
| Core | 36 |
| Agg1 | 47 |
| Agg2 | 57 |
| SW0 | 46 |
| SW1 | 41 |
| SW2 | 46 |
| SW3 | 46 |
| SW4 | 41 |
| PC0 | —（主机，无 CLI 配置） |
| PC1 | —（主机，无 CLI 配置） |
| PC2 | —（主机，无 CLI 配置） |
| PC3 | —（主机，无 CLI 配置） |
| PC4 | —（主机，无 CLI 配置） |
| PC5 | —（主机，无 CLI 配置） |
| PC6 | —（主机，无 CLI 配置） |
| PC7 | —（主机，无 CLI 配置） |
| SRV0 | —（主机，无 CLI 配置） |
| SRV1 | —（主机，无 CLI 配置） |
| SRV2 | —（主机，无 CLI 配置） |

<details><summary>展开全部下发配置</summary>

**Router0**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname Router0
!
no ip cef
no ipv6 cef
!
interface FastEthernet2/0
 ip address 192.168.3.1 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 no ip address
 no shutdown
!
interface Serial0/0
 ip address 200.1.1.2 255.255.255.252
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
ip route 0.0.0.0 0.0.0.0 200.1.1.1
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

**Router1**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname Router1
!
no ip cef
no ipv6 cef
!
interface FastEthernet2/0
 ip address 218.75.230.1 255.255.255.0
 no shutdown
!
interface FastEthernet3/0
 no ip address
 no shutdown
!
interface Serial0/0
 ip address 200.1.1.1 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
ip route 0.0.0.0 0.0.0.0 200.1.1.2
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

**Core**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname Core
!
no ip cef
no ipv6 cef
!
interface FastEthernet2/0
 ip address 192.168.3.2 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 ip address 192.168.2.2 255.255.255.252
 no shutdown
!
interface Serial0/0
 ip address 192.168.1.2 255.255.255.252
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
ip route 0.0.0.0 0.0.0.0 192.168.3.1
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

**Agg1**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname Agg1
!
no ip cef
no ipv6 cef
!
interface FastEthernet2/0
 no ip address
 no shutdown
!
interface FastEthernet2/0.10
 encapsulation dot1Q 10
 ip address 192.168.10.254 255.255.255.0
 no shutdown
!
interface FastEthernet2/0.20
 encapsulation dot1Q 20
 ip address 192.168.20.254 255.255.255.0
 no shutdown
!
interface FastEthernet3/0
 no ip address
 no shutdown
!
interface Serial0/0
 ip address 192.168.1.1 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
ip route 0.0.0.0 0.0.0.0 192.168.1.2
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

**Agg2**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname Agg2
!
no ip cef
no ipv6 cef
!
access-list 100 deny ip 192.168.30.0 0.0.0.255 192.168.50.0 0.0.0.255
access-list 100 deny ip 192.168.40.0 0.0.0.255 192.168.50.0 0.0.0.255
access-list 100 permit ip 0.0.0.0 255.255.255.255 0.0.0.0 255.255.255.255
!
interface FastEthernet2/0
 no ip address
 no shutdown
!
interface FastEthernet2/0.30
 encapsulation dot1Q 30
 ip address 192.168.30.254 255.255.255.0
 ip access-group 100 in
 no shutdown
!
interface FastEthernet2/0.40
 encapsulation dot1Q 40
 ip address 192.168.40.254 255.255.255.0
 ip access-group 100 in
 no shutdown
!
interface FastEthernet2/0.50
 encapsulation dot1Q 50
 ip address 192.168.50.254 255.255.255.0
 no shutdown
!
interface FastEthernet3/0
 ip address 192.168.2.1 255.255.255.252
 no shutdown
!
interface Serial0/0
 no ip address
 shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
ip route 0.0.0.0 0.0.0.0 192.168.2.2
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

**SW0**

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
vlan 10
 name DEPT-A
!
vlan 20
 name DEPT-B
!
vlan 30
 name DEPT-C
!
vlan 40
 name DEPT-D
!
vlan 50
 name IDC
!
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 10
 no shutdown
!
interface FastEthernet0/2
 switchport mode access
 switchport access vlan 10
 no shutdown
!
interface FastEthernet0/3
 switchport mode trunk
 switchport trunk allowed vlan 10,20
 no shutdown
!
interface FastEthernet0/4
 switchport mode trunk
 switchport trunk allowed vlan 20
 no shutdown
!
end
```

**SW1**

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
vlan 10
 name DEPT-A
!
vlan 20
 name DEPT-B
!
vlan 30
 name DEPT-C
!
vlan 40
 name DEPT-D
!
vlan 50
 name IDC
!
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 20
 no shutdown
!
interface FastEthernet0/2
 switchport mode access
 switchport access vlan 20
 no shutdown
!
interface FastEthernet0/3
 switchport mode trunk
 switchport trunk allowed vlan 20
 no shutdown
!
end
```

**SW2**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname SW2
!
no ip cef
no ipv6 cef
!
vlan 10
 name DEPT-A
!
vlan 20
 name DEPT-B
!
vlan 30
 name DEPT-C
!
vlan 40
 name DEPT-D
!
vlan 50
 name IDC
!
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 30
 no shutdown
!
interface FastEthernet0/2
 switchport mode access
 switchport access vlan 30
 no shutdown
!
interface FastEthernet0/3
 switchport mode trunk
 switchport trunk allowed vlan 30,40,50
 no shutdown
!
interface FastEthernet0/4
 switchport mode trunk
 switchport trunk allowed vlan 40,50
 no shutdown
!
end
```

**SW3**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname SW3
!
no ip cef
no ipv6 cef
!
vlan 10
 name DEPT-A
!
vlan 20
 name DEPT-B
!
vlan 30
 name DEPT-C
!
vlan 40
 name DEPT-D
!
vlan 50
 name IDC
!
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 40
 no shutdown
!
interface FastEthernet0/2
 switchport mode access
 switchport access vlan 40
 no shutdown
!
interface FastEthernet0/3
 switchport mode trunk
 switchport trunk allowed vlan 40,50
 no shutdown
!
interface FastEthernet0/4
 switchport mode trunk
 switchport trunk allowed vlan 50
 no shutdown
!
end
```

**SW4**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname SW4
!
no ip cef
no ipv6 cef
!
vlan 10
 name DEPT-A
!
vlan 20
 name DEPT-B
!
vlan 30
 name DEPT-C
!
vlan 40
 name DEPT-D
!
vlan 50
 name IDC
!
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 50
 no shutdown
!
interface FastEthernet0/2
 switchport mode access
 switchport access vlan 50
 no shutdown
!
interface FastEthernet0/3
 switchport mode trunk
 switchport trunk allowed vlan 50
 no shutdown
!
end
```

**PC0**

（主机，地址在图形界面里配，无 CLI 配置）

**PC1**

（主机，地址在图形界面里配，无 CLI 配置）

**PC2**

（主机，地址在图形界面里配，无 CLI 配置）

**PC3**

（主机，地址在图形界面里配，无 CLI 配置）

**PC4**

（主机，地址在图形界面里配，无 CLI 配置）

**PC5**

（主机，地址在图形界面里配，无 CLI 配置）

**PC6**

（主机，地址在图形界面里配，无 CLI 配置）

**PC7**

（主机，地址在图形界面里配，无 CLI 配置）

**SRV0**

（主机，地址在图形界面里配，无 CLI 配置）

**SRV1**

（主机，地址在图形界面里配，无 CLI 配置）

**SRV2**

（主机，地址在图形界面里配，无 CLI 配置）

</details>

## 验证结果

| 指标 | 数值 |
|---|---|
| 推算主机对 | 110 组（通 102 / 不通 8） |
| 断言 | 14 条，未通过 0 条 |
| 一致性检查 | 34 项，未通过 0 项 |

> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。
> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。
