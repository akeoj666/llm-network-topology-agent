# 生成审计日志

- 需求原文：家里两台电脑想连起来互传文件，就一台交换机，192.168.1.0/24，不用路由器，要求两台能互通。

- 模型：（YAML 通道，无模型参与）
- 通道：YAML 直读
- 结果：通过

## 命令级筛查

| 指标 | 数值 |
|---|---|
| 配置行 | 20 行，3 台设备 |
| 拦截 / 告警 | 0 / 0 |

> 全部命令都在白名单内，未命中黑名单。

## 部署确认

- 确认来源：**命令行参数 --confirm**
- 确认对象：3 台设备 / 20 行配置

## 下发内容

- 产物：`UC-06.pkt`
- 设备 3 台，合计 20 行配置

| 设备 | 配置行数 |
|---|---|
| SW0 | 20 |
| PC0 | —（主机，无 CLI 配置） |
| PC1 | —（主机，无 CLI 配置） |

<details><summary>展开全部下发配置</summary>

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
interface FastEthernet0/2
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
| 一致性检查 | 1 项，未通过 0 项 |

> 口径：这是**从拓扑模型推算**的预期结果，不是 Packet Tracer 实跑。
> 提交前应按报告里的「预期通 / 预期不通」各抽 2 条在 PT 里核对。
