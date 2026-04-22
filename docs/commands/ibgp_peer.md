# 命令：ibgp peer

## `dn42ctl ibgp peer`（等价于 `dn42ctl ibgp peer add`）

用途：创建内网 iBGP peer。可选择是否同时创建 WireGuard 隧道。

### 输入

- 必填：`--name`、`--peer-ip`（对端网内 IPv6 地址）。
- WG 相关（`--no-wg` 未设置时必填）：`--pubkey`、`--peer-lla`、`--net`、`--rxcost`。
- 可选：
  - `--endpoint`：对端 Endpoint（IP:Port）。可为空，适用于对端在防火墙后的场景。
  - `--listen-port`：
    - `0` 表示不设置
    - 留空则自动选择未占用端口（避免与当前节点已有端口冲突）
  - `--no-wg`：跳过 WireGuard 隧道创建。不生成密钥、不写网络配置文件、不修改 babel.conf。

> `--rxcost` 未提供时，CLI 应通过交互提示要求用户输入。

> 交互模式下（有 WG 时）：如果 `--pubkey/--endpoint/--peer-lla` 缺失，CLI 会先生成并输出本端 WG 公钥与本端 LLA，便于先发给对端；随后再提示输入对端信息。其中 `--endpoint` 允许留空。

### 派生规则

- `ifname`：`wg_<sanitize(name)>`（长度不得超过 15）——仅在有 WG 时有意义
- `ListenPort`：默认从高端口随机选择且避免冲突；也允许通过 `--listen-port` 覆盖。无 WG 时为 0。
- `rxcost`：写入 DB（`ibgp_peers.babel_rxcost`），并用于生成 `babel.conf` 的对应 `interface` 段。

### 输出

- Bird iBGP peer conf：始终写入 `bird_peers_dir/ibgp_<name>.conf`。使用 `--peer-ip`（网内 IP）作为 neighbor 地址。
- 有 WG 时：写入 networkd 或 NetworkManager 的 WireGuard 配置文件，**重生成** `babel.conf`。
- 无 WG 时：仅写入 Bird peer conf，不写网络配置文件，不修改 babel.conf。

---

## `dn42ctl ibgp peer modify`

用途：修改已存在 iBGP peer 的 Babel `rxcost`（不删除 peer），并重生成 `babel.conf`。

### 输入

- 必填：`<name>`
- 必填：`--rxcost`（未提供时应交互提示）

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
- 删除数据库记录。
- 有 WG 时：删除生成文件（Bird peer conf + networkd/NM 文件），并重生成 `babel.conf`。
- 无 WG 时：仅删除 Bird peer conf。