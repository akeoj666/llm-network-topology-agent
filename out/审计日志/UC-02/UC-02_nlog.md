# 生成审计日志

- 需求原文：三台路由器三角形互联，每台下面各挂一个部门网段，原来跑 RIP，现在要换成 OSPF 单区域，要求任意两台 PC 都能通，并且断一条串口链路后还能自动绕路。

- 模型：（YAML 通道，无模型参与）
- 通道：YAML 直读
- 结果：通过

## 命令级筛查

| 指标 | 数值 |
|---|---|
| 配置行 | 171 行，9 台设备 |
| 拦截 / 告警 | 0 / 0 |

> 全部命令都在白名单内，未命中黑名单。

## 部署确认

- 确认来源：**命令行参数 --confirm**
- 确认对象：9 台设备 / 171 行配置

## 下发内容

- 产物：`UC-02.pkt`
- 设备 9 台，合计 171 行配置

| 设备 | 配置行数 |
|---|---|
| R1 | 39 |
| R2 | 40 |
| R3 | 41 |
| SW0 | 17 |
| SW1 | 17 |
| SW2 | 17 |
| PC0 | —（主机，无 CLI 配置） |
| PC1 | —（主机，无 CLI 配置） |
| PC2 | —（主机，无 CLI 配置） |

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
 ip address 192.168.10.1 255.255.255.0
 no shutdown
!
interface Serial0/0
 ip address 10.0.0.1 255.255.255.252
 no shutdown
!
interface Serial1/0
 ip address 10.0.0.9 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router ospf 100
 network 192.168.10.0 0.0.0.255 area 0
 network 10.0.0.0 0.0.0.3 area 0
 network 10.0.0.8 0.0.0.3 area 0
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
 ip address 192.168.20.1 255.255.255.0
 no shutdown
!
interface Serial1/0
 ip address 10.0.0.2 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial0/0
 ip address 10.0.0.5 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router ospf 100
 network 192.168.20.0 0.0.0.255 area 0
 network 10.0.0.0 0.0.0.3 area 0
 network 10.0.0.4 0.0.0.3 area 0
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

**R3**

```
version 12.2
no service timestamps log datetime msec
no service timestamps debug datetime msec
no service password-encryption
!
hostname R3
!
no ip cef
no ipv6 cef
!
interface FastEthernet2/0
 ip address 192.168.30.1 255.255.255.0
 no shutdown
!
interface Serial1/0
 ip address 10.0.0.6 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial0/0
 ip address 10.0.0.10 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router ospf 100
 network 192.168.30.0 0.0.0.255 area 0
 network 10.0.0.4 0.0.0.3 area 0
 network 10.0.0.8 0.0.0.3 area 0
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
interface FastEthernet0/1
 no shutdown
!
interface FastEthernet0/24
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
interface FastEthernet0/1
 no shutdown
!
interface FastEthernet0/24
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
interface FastEthernet0/1
 no shutdown
!
interface FastEthernet0/24
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

</details>

## 验证结果

| 指标 | 数值 |
|---|---|
| 推算主机对 | 6 组（通 6 / 不通 0） |
| 断言 | 6 条，未通过 0 条 |
| 一致性检查 | 6 项，未通过 0 项 |

> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。
> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。
