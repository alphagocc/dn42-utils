# 网络后端（networkd / NetworkManager）

## 总体约束

- peer WireGuard 配置（`bgp peer` / `ibgp peer`）仅支持 `systemd-networkd`。
- `dummy_backend` 仍支持 `networkd` 和 `nm`（NetworkManager）。
- WireGuard 的 `AllowedIPs` 必须写入（配置完整性）。
- 但必须**禁止**因 `AllowedIPs` 自动修改系统路由表。

该约束的实现方式如下：

- networkd：显式设置 `RouteTable=off`

工具不负责自动添加任何 DN42 路由策略；如需路由，请用户在系统层面自行管理。

## systemd-networkd

- 输出目录：通常为 `/etc/systemd/network/`（也允许由参数覆盖）。
- `.netdev`：
  - 使用 `Kind=wireguard`
  - 必须设置：`RouteTable=off`
  - 文件权限：`0640 root:systemd-network`（包含 WireGuard 私钥，需要让 systemd-networkd 可读）
- `.network`：
  - 为接口配置 LLA 地址
  - 配置对端的 `Peer=<peer_lla>` 等必要信息

## NetworkManager（仅 dummy_backend，已废弃用于 peer）

> **注意**：以下内容仅适用于 `dummy_backend = "nm"` 场景。peer WireGuard 配置已不再支持 NetworkManager。

- 输出目录：通常为 `/etc/NetworkManager/system-connections/`（也允许由参数覆盖）。
- 文件格式：keyfile（`.nmconnection`），`type=wireguard`。
- 必须设置：
  - `[wireguard] peer-routes=false`
- peer 配置使用独立的 `[wireguard-peer.<PUBLIC_KEY>]` section。
- `allowed-ips`：多 CIDR 使用 `;` 分隔，末尾带 `;`。
- `persistent-keepalive`：可选，写入 peer section。
- `endpoint`：可选，写入 peer section。

示例结构：

```ini
[wireguard]
private-key=...
listen-port=51820
peer-routes=false

[wireguard-peer.<PUBLIC_KEY>]
endpoint=<host>:<port>
allowed-ips=fe80::/64;fd00::/8;
persistent-keepalive=25
```

### 稳定 UUID

- `connection.uuid` 需要稳定：基于 `node_id + ifname` 生成确定性 UUIDv5，避免“重新生成导致新连接”的问题。

## dn42-dummy 接口

目前 `dn42-dummy` 是唯一同时支持 `networkd` 与 `nm` 两种后端的接口，由 `config.toml` 的 `dummy_backend` 字段选择。
创建行为与失败处理详见 [`../commands/init.md`](../commands/init.md)。

## 相关文档

- 默认输出路径与权限：[`paths.md`](paths.md)
- 已移除的 peer 级 NM 后端：[`../deprecated.md`](../deprecated.md)
