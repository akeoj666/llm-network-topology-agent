# 生成审计日志

- 需求原文：财务部和市场部要在二层隔离、三层互通，一台交换机接 6 台 PC，VLAN10 用 192.168.1.0/24、VLAN20 用 192.168.2.0/24，各 3 台，网关都是 .254，用单臂路由实现跨 VLAN 互通，同 VLAN 内也要通。

- 模型：（YAML 通道，无模型参与）
- 通道：YAML 直读
- 结果：通过

## 命令级筛查

| 指标 | 数值 |
|---|---|
| 配置行 | 95 行，8 台设备 |
| 拦截 / 告警 | 0 / 0 |

> 全部命令都在白名单内，未命中黑名单。

## 部署确认

- 确认来源：**命令行参数 --confirm**
- 确认对象：8 台设备 / 95 行配置

## 下发内容

- 产物：`UC-05.pkt`
- 设备 8 台，合计 95 行配置

| 设备 | 配置行数 |
|---|---|
| R1 | 44 |
| SW0 | 51 |
| PC0 | —（主机，无 CLI 配置） |
| PC1 | —（主机，无 CLI 配置） |
| PC2 | —（主机，无 CLI 配置） |
| PC3 | —（主机，无 CLI 配置） |
| PC4 | —（主机，无 CLI 配置） |
| PC5 | —（主机，无 CLI 配置） |

<details><summary>展开全部下发配置</summary>

**R1**

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
interface FastEthernet2/0
 no ip address
 no shutdown
!
interface FastEthernet2/0.10
 encapsulation dot1Q 10
 ip address 192.168.1.254 255.255.255.0
 no shutdown
!
interface FastEthernet2/0.20
 encapsulation dot1Q 20
 ip address 192.168.2.254 255.255.255.0
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
interface FastEthernet3/0
 no ip address
 shutdown
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
 name FIN
!
vlan 20
 name MKT
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
 switchport mode access
 switchport access vlan 10
 no shutdown
!
interface FastEthernet0/4
 switchport mode access
 switchport access vlan 20
 no shutdown
!
interface FastEthernet0/5
 switchport mode access
 switchport access vlan 20
 no shutdown
!
interface FastEthernet0/6
 switchport mode access
 switchport access vlan 20
 no shutdown
!
interface FastEthernet0/24
 switchport mode trunk
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

</details>

## 验证结果

| 指标 | 数值 |
|---|---|
| 推算主机对 | 30 组（通 30 / 不通 0） |
| 断言 | 12 条，未通过 0 条 |
| 一致性检查 | 13 项，未通过 0 项 |

> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。
> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。
