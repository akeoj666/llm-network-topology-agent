# UC-93 双路由 OSPF 单区域

> 需求原文：两台路由器用串口线互连，各带一个 192.168.77.0/24 和 192.168.88.0/24 网段，各挂一台交换机和一台 PC，跑 OSPF 单区域

## 接线表

| 设备 | 端口 | 对端设备 | 对端端口 | 线型 |
|---|---|---|---|---|
| R1 | Serial0/0 | R2 | Serial1/0 | 串口线 |
| PC1 | FastEthernet0 | SW1 | FastEthernet0/1 | 直通线 |
| SW1 | FastEthernet0/24 | R1 | FastEthernet2/0 | 直通线 |
| PC2 | FastEthernet0 | SW2 | FastEthernet0/1 | 直通线 |
| SW2 | FastEthernet0/24 | R2 | FastEthernet2/0 | 直通线 |

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
 ip address 192.168.77.1 255.255.255.0
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
 network 192.168.77.0 0.0.0.255 area 0
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
interface FastEthernet2/0
 ip address 192.168.88.1 255.255.255.0
 no shutdown
!
interface Serial1/0
 ip address 10.0.0.2 255.255.255.252
 clock rate 2000000
 no shutdown
!
interface Serial0/0
 no ip address
 shutdown
!
interface FastEthernet3/0
 no ip address
 shutdown
!
router ospf 1
 network 192.168.88.0 0.0.0.255 area 0
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

## SW2（switch）

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

## PC1（主机）

- IP：192.168.77.10/24
- 网关：192.168.77.1

## PC2（主机）

- IP：192.168.88.10/24
- 网关：192.168.88.1
