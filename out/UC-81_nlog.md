# 生成审计日志

- 需求原文：三台路由器两个 PC，左边 192.168.16.0/25 带一台 PC（网关 .126），右边 192.168.16.128/26 带一台 PC（网关 .190），两台边缘路由器之间除了串口直连还有一条绕行路径，全网跑 RIP 第二版并关闭自动汇总，看两个 PC 能不能互通。

- 模型：（YAML 通道，无模型参与）
- 通道：YAML 直读
- 结果：通过

## 命令级筛查

| 指标 | 数值 |
|---|---|
| 配置行 | 157 行，7 台设备 |
| 拦截 / 告警 | 0 / 0 |

> 全部命令都在白名单内，未命中黑名单。

## 部署确认

- 确认来源：**命令行参数 --confirm**
- 确认对象：7 台设备 / 157 行配置

## 下发内容

- 产物：`UC-81.pkt`
- 设备 7 台，合计 157 行配置

| 设备 | 配置行数 |
|---|---|
| R0 | 41 |
| R1 | 42 |
| R2 | 40 |
| SW0 | 17 |
| SW1 | 17 |
| PC0 | —（主机，无 CLI 配置） |
| PC1 | —（主机，无 CLI 配置） |

<details><summary>展开全部下发配置</summary>

**R0**

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
 ip address 192.168.16.126 255.255.255.128
 no shutdown
!
interface FastEthernet3/0
 ip address 10.0.0.1 255.255.255.252
 no shutdown
!
interface Serial0/0
 ip address 30.0.0.1 255.255.255.252
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
router rip
 version 2
 no auto-summary
 network 192.168.16.0
 network 10.0.0.0
 network 30.0.0.0
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
 ip address 192.168.16.190 255.255.255.192
 no shutdown
!
interface FastEthernet3/0
 ip address 20.0.0.1 255.255.255.252
 no shutdown
!
interface Serial0/0
 ip address 30.0.0.2 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial1/0
 no ip address
 shutdown
!
router rip
 version 2
 no auto-summary
 network 192.168.16.0
 network 20.0.0.0
 network 30.0.0.0
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
 ip address 10.0.0.2 255.255.255.252
 no shutdown
!
interface FastEthernet3/0
 ip address 20.0.0.2 255.255.255.252
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
router rip
 version 2
 no auto-summary
 network 10.0.0.0
 network 20.0.0.0
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

**PC0**

（主机，地址在图形界面里配，无 CLI 配置）

**PC1**

（主机，地址在图形界面里配，无 CLI 配置）

</details>

## 验证结果

| 指标 | 数值 |
|---|---|
| 推算主机对 | 2 组（通 2 / 不通 0） |
| 断言 | 2 条，未通过 0 条 |
| 一致性检查 | 5 项，未通过 0 项 |

> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。
> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。
