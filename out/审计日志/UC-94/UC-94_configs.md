# UC-94 ACL 隔离并放行服务器

> 需求原文：研发部 192.168.30.0/24 和财务部 192.168.40.0/24 各挂一台交换机和一台 PC，另有一台服务器在 100.100.100.0/24，两台路由器串口相连。要求研发部不能访问财务部，但两边都能访问服务器，用 OSPF 加 ACL 实现

## 接线表

| 设备 | 端口 | 对端设备 | 对端端口 | 线型 |
|---|---|---|---|---|
| R1 | Serial0/0 | R2 | Serial1/0 | 串口线 |
| PC0 | FastEthernet0 | SW0 | FastEthernet0/1 | 直通线 |
| SW0 | FastEthernet0/24 | R1 | FastEthernet2/0 | 直通线 |
| PC1 | FastEthernet0 | SW1 | FastEthernet0/1 | 直通线 |
| SW1 | FastEthernet0/24 | R2 | FastEthernet2/0 | 直通线 |
| Server0 | FastEthernet0 | SW2 | FastEthernet0/1 | 直通线 |
| SW2 | FastEthernet0/24 | R2 | FastEthernet3/0 | 直通线 |

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
access-list 100 deny ip 192.168.30.0 0.0.0.255 192.168.40.0 0.0.0.255
access-list 100 permit ip any any
!
interface FastEthernet2/0
 ip address 192.168.30.1 255.255.255.0
 ip access-group 100 in
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
 network 192.168.30.0 0.0.0.255 area 0
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
 ip address 192.168.40.1 255.255.255.0
 no shutdown
!
interface FastEthernet3/0
 ip address 100.100.100.1 255.255.255.0
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
router ospf 1
 network 192.168.40.0 0.0.0.255 area 0
 network 100.100.100.0 0.0.0.255 area 0
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

## PC0（主机）

- IP：192.168.30.10/24
- 网关：192.168.30.1

## PC1（主机）

- IP：192.168.40.10/24
- 网关：192.168.40.1

## Server0（主机）

- IP：100.100.100.10/24
- 网关：100.100.100.1
