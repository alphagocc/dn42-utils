# 网络后端（networkd / NetworkManager）

## 总体约束

- 工具必须同时支持：
  - `systemd-networkd`
  - `NetworkManager`
- WireGuard 的 `AllowedIPs` 必须写入（配置完整性）。
- 但必须**禁止**因 `AllowedIPs` 自动修改系统路由表。

该约束的实现方式如下：

- networkd：显式设置 `RouteTable=off`
- NetworkManager：显式设置 `peer-routes=false`

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

## NetworkManager

- 输出目录：通常为 `/etc/NetworkManager/system-connections/`（也允许由参数覆盖）。
- 文件格式：keyfile（`.nmconnection`），`type=wireguard`。
- 必须设置：
  - `[wireguard] peer-routes=false`
- `allowed-ips`：多 CIDR 使用 `;` 分隔（NetworkManager wireguard peers 语法）。

### 稳定 UUID

- `connection.uuid` 需要稳定：基于 `node_id + ifname` 生成确定性 UUIDv5，避免“重新生成导致新连接”的问题。
