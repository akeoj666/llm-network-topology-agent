# UC-92 三路由 RIP VLSM

> 需求原文：三台路由器两台PC，左边 192.168.16.0/24 前一半给行政部，右边 192.168.16.128/26 给研发部，路由器之间三条 /30 串联，跑 RIP 要支持 VLSM 并关闭自动汇总

## 接线表

| 设备 | 端口 | 对端设备 | 对端端口 | 线型 |
|---|---|---|---|---|
| R1 | Serial0/0 | R2 | Serial0/0 | 串口线 |
| R2 | Serial1/0 | R3 | Serial0/0 | 串口线 |
| R1 | Serial1/0 | R3 | Serial1/0 | 串口线 |
| PC0 | FastEthernet0 | SW0 | FastEthernet0/1 | 直通线 |
| SW0 | FastEthernet0/24 | R1 | FastEthernet2/0 | 直通线 |
| PC1 | FastEthernet0 | SW1 | FastEthernet0/1 | 直通线 |
| SW1 | FastEthernet0/24 | R3 | FastEthernet2/0 | 直通线 |

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
interface FastEthernet2/0
 ip address 192.168.16.1 255.255.255.128
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
router rip
 version 2
 no auto-summary
 network 192.168.16.0
 network 10.0.0.0
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
interface Serial0/0
 ip address 10.0.0.2 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial1/0
 ip address 10.0.0.5 255.255.255.252
 no shutdown
!
interface FastEthernet2/0
 no ip address
 shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router rip
 version 2
 no auto-summary
 network 10.0.0.0
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

## R3（router）

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
 ip address 192.168.16.129 255.255.255.192
 no shutdown
!
interface Serial0/0
 ip address 10.0.0.6 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial1/0
 ip address 10.0.0.10 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router rip
 version 2
 no auto-summary
 network 192.168.16.0
 network 10.0.0.0
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

- IP：192.168.16.10/25
- 网关：192.168.16.1

## PC1（主机）

- IP：192.168.16.138/26
- 网关：192.168.16.129
