# UC-95 单臂路由 VLAN 互通

> 需求原文：一台路由器用一根网线接一台交换机，交换机上划 VLAN 10 给研发 192.168.10.0/24 和 VLAN 20 给财务 192.168.20.0/24，各挂三台 PC，用单臂路由让两个 VLAN 之间能互通

## 接线表

| 设备 | 端口 | 对端设备 | 对端端口 | 线型 |
|---|---|---|---|---|
| SW0 | FastEthernet0/24 | R1 | FastEthernet2/0 | 直通线 |
| PC0 | FastEthernet0 | SW0 | FastEthernet0/1 | 直通线 |
| PC1 | FastEthernet0 | SW0 | FastEthernet0/2 | 直通线 |
| PC2 | FastEthernet0 | SW0 | FastEthernet0/3 | 直通线 |
| PC3 | FastEthernet0 | SW0 | FastEthernet0/4 | 直通线 |
| PC4 | FastEthernet0 | SW0 | FastEthernet0/5 | 直通线 |
| PC5 | FastEthernet0 | SW0 | FastEthernet0/6 | 直通线 |

## VLAN 规划

| VLAN | 名称 | 网段 |
|---|---|---|
| 10 | RD | 192.168.10.0/24 |
| 20 | FIN | 192.168.20.0/24 |

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
 no ip address
 no shutdown
!
interface FastEthernet2/0.10
 encapsulation dot1Q 10
 ip address 192.168.10.1 255.255.255.0
 no shutdown
!
interface FastEthernet2/0.20
 encapsulation dot1Q 20
 ip address 192.168.20.1 255.255.255.0
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
vlan 10
 name RD
!
vlan 20
 name FIN
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

## PC0（主机）

- IP：192.168.10.10/24
- 网关：192.168.10.1

## PC1（主机）

- IP：192.168.10.11/24
- 网关：192.168.10.1

## PC2（主机）

- IP：192.168.10.12/24
- 网关：192.168.10.1

## PC3（主机）

- IP：192.168.20.10/24
- 网关：192.168.20.1

## PC4（主机）

- IP：192.168.20.11/24
- 网关：192.168.20.1

## PC5（主机）

- IP：192.168.20.12/24
- 网关：192.168.20.1
