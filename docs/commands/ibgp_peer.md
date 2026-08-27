# 命令：ibgp peer

## 设计要点

iBGP 互联与 eBGP（`bgp peer`）在几处刻意不同，修改前请先理解原因：

- **neighbor 地址使用网内 IP**：iBGP peer 以 `--peer-ip`（网内 IPv6）作为 Bird neighbor 地址。iBGP 内网已有 babel 路由协议，无需依赖 LLA 互联。
- **支持无 WireGuard 模式**（`--no-wg`）：仅生成 Bird peer conf，不创建 WG 隧道、不修改 `babel.conf`。适用于对端已通过其他方式（物理网络、已有隧道）可达的场景。
- **`endpoint` 可选**：对端可能在防火墙后，无需填写。
- **AllowedIPs 默认值更宽**：iBGP 对端均为可信任的自有机器，因此隧道默认放行 `fe80::/64, fd00::/8, ff02::/16`，涵盖 link-local、DN42 与组播流量；eBGP peer 默认为 `fe80::/64, fd00::/8`。两者均可通过 `--allowed-ips` 覆盖。

> `rxcost` / `type` 如何写入 `babel.conf`，见 [`../architecture/babel.md`](../architecture/babel.md)。

## `dn42ctl ibgp peer`（等价于 `dn42ctl ibgp peer add`）

用途：创建内网 iBGP peer。可选择是否同时创建 WireGuard 隧道。

### 输入

- 必填：`--name`、`--peer-ip`（对端网内 IPv6 地址）。
- WG 相关（`--no-wg` 未设置时必填）：`--pubkey`、`--peer-lla`、`--rxcost`。
- 可选：
  - `--endpoint`：对端 Endpoint（IP:Port）。可为空，适用于对端在防火墙后的场景。
  - `--type`：Babel interface type，取值 `wired`/`wireless`/`tunnel`，默认 `tunnel`。高丢包链路建议使用 `wireless`。
  - `--listen-port`：
    - `0` 表示不设置
    - 留空则自动选择未占用端口（避免与当前节点已有端口冲突）
  - `--no-wg`：跳过 WireGuard 隧道创建。不生成密钥、不写网络配置文件、不修改 babel.conf。

### 输入校验

- `--name`：非空，自动规范化（非字母数字下划线字符替换为 `_`，转小写）。
- `--peer-ip`：合法的 IPv6 地址（允许带 `/prefix`）。
- `--pubkey`：WireGuard 公钥，base64 格式，解码后须为 32 字节。
- `--endpoint`：`host:port` 或 `[IPv6]:port` 格式，端口 1-65535。可为空。
- `--peer-lla`：合法的 IPv6 地址（允许带 `/prefix`）。
- `--rxcost`：0-65535。
- `--type`：`wired` / `wireless` / `tunnel`（大小写不敏感）。
- `--listen-port`：0 或 1-65535。

> `--rxcost` 未提供时，CLI 应通过交互提示要求用户输入。

> 交互模式下（有 WG 时）：如果 `--pubkey/--endpoint/--peer-lla` 缺失，CLI 会先生成并输出本端 WG 公钥与本端 LLA，便于先发给对端；随后再提示输入对端信息。其中 `--endpoint` 允许留空。

### 派生规则

- `ifname`：`wg_<sanitize(name)>`，长度不得超过 15，仅在有 WG 隧道时有意义。
- `ListenPort`：默认从高端口随机选择且避免冲突；也允许通过 `--listen-port` 覆盖。无 WG 时为 0。
- `rxcost`：写入 DB（`ibgp_peers.babel_rxcost`），并用于生成 `babel.conf` 的对应 `interface` 段。
- `type`：写入 DB（`ibgp_peers.babel_type`），用于生成 `babel.conf` 的 `type` 字段。默认 `tunnel`。

### 输出

- Bird iBGP peer conf：始终写入 `bird_peers_dir/ibgp_<name>.conf`。使用 `--peer-ip`（网内 IP）作为 neighbor 地址。
- 有 WG 时：写入 networkd 的 WireGuard 配置文件，**重生成** `babel.conf`。
- 无 WG 时：仅写入 Bird peer conf，不写网络配置文件，不修改 babel.conf。`--no-wg` peer 不会出现在 `babel.conf` 的 interface 列表中。

---

## `dn42ctl ibgp peer modify`

用途：修改已存在 iBGP peer 的 WG 相关参数（pubkey/endpoint/peer_lla/peer_ip/rxcost/listen_port/type），并重生成配置文件。

### 输入

- 必填：`<name>`
- 可选：`--rxcost`、`--type`（未提供时应交互提示）

### 行为

- 更新 DB 中该 peer 的 `babel_rxcost`。
- **重生成** `babel.conf`（确定性、幂等）。

---

## `dn42ctl ibgp peer del`

用途：删除指定的 iBGP peer。

### 输入

- 必填：`<name>`

### 行为

- 删除前必须二次确认（交互 prompt）。
- 先删数据库记录，再删生成文件。顺序见 [`../architecture/database.md`](../architecture/database.md)。
- 有 WG 时：删除 Bird peer conf + networkd 文件，并重生成 `babel.conf`。
- 无 WG 时：仅删除 Bird peer conf。
- 删不掉的文件（权限等）只打 warning，不让命令失败——DB 行已经删了，退不回去。
