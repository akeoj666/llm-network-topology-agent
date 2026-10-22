# 生成审计日志

- 需求原文：咖啡馆的客人 WiFi 和收银机要在二层隔离，客人网段 192.168.30.0/24，收银 192.168.40.0/24，都接在一台交换机上再单臂接到路由器，路由器外口接 ISP 能上网。要求客人能上网但不能访问收银网络，收银不受限制。

- 模型：（YAML 通道，无模型参与）
- 通道：YAML 直读
- 结果：通过

## 命令级筛查

| 指标 | 数值 |
|---|---|
| 配置行 | 128 行，8 台设备 |
| 拦截 / 告警 | 0 / 0 |

> 全部命令都在白名单内，未命中黑名单。

## 部署确认

- 确认来源：**命令行参数 --confirm**
- 确认对象：8 台设备 / 128 行配置

## 下发内容

- 产物：`UC-07.pkt`
- 设备 8 台，合计 128 行配置

| 设备 | 配置行数 |
|---|---|
| R1 | 50 |
| R2 | 37 |
| SW0 | 41 |
| PC0 | —（主机，无 CLI 配置） |
| PC1 | —（主机，无 CLI 配置） |
| PC2 | —（主机，无 CLI 配置） |
| PC3 | —（主机，无 CLI 配置） |
| SRV0 | —（主机，无 CLI 配置） |

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
access-list 100 deny ip 192.168.30.0 0.0.0.255 192.168.40.0 0.0.0.255
access-list 100 permit ip any any
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
 no shutdown
!
interface FastEthernet3/0
 ip address 202.96.0.1 255.255.255.252
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
ip route 0.0.0.0 0.0.0.0 202.96.0.2
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

**R2**

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
interface FastEthernet2/0
 ip address 202.96.0.2 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 ip address 218.75.230.1 255.255.255.0
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
ip route 192.168.30.0 255.255.255.0 202.96.0.1
ip route 192.168.40.0 255.255.255.0 202.96.0.1
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
vlan 30
 name GUEST
!
vlan 40
 name CASHIER
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
 switchport mode access
 switchport access vlan 40
 no shutdown
!
interface FastEthernet0/4
 switchport mode access
 switchport access vlan 40
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

**SRV0**

（主机，地址在图形界面里配，无 CLI 配置）

</details>

## 验证结果

| 指标 | 数值 |
|---|---|
| 推算主机对 | 20 组（通 16 / 不通 4） |
| 断言 | 10 条，未通过 0 条 |
| 一致性检查 | 12 项，未通过 0 项 |

> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。
> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。
