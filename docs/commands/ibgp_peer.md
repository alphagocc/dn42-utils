# 命令：ibgp peer

## `dn42ctl ibgp peer`

用途：创建内网 iBGP peer（wireguard 隧道 + bird peers + babel 互联）。

### 输入

- 必填：`--name`、`--pubkey`、`--endpoint`、`--peer-lla`、`--net`、`--rxcost`。
- 可选：`--listen-port`
  - `0` 表示不设置
  - 留空则自动选择未占用端口（避免与当前节点已有端口冲突）

> `--rxcost` 未提供时，CLI 应通过交互提示要求用户输入。

### 派生规则

- `ifname`：`wg_<sanitize(name)>`（长度不得超过 15）
- `ListenPort`：默认从高端口随机选择且避免冲突；也允许通过 `--listen-port` 覆盖
- `rxcost`：写入 DB（`ibgp_peers.babel_rxcost`），并用于生成 `babel.conf` 的对应 `interface` 段。

### 输出

- Bird iBGP peer conf：写入 `bird_peers_dir/ibgp_<name>.conf`。
- 写入 networkd 或 NetworkManager 的 wireguard 配置文件。
- **重生成** `babel.conf`：从数据库读取该节点所有 iBGP peer 的接口列表与各自的 `rxcost`，确定性、幂等地生成。

---

## `dn42ctl ibgp peer modify`

用途：修改已存在 iBGP peer 的 Babel `rxcost`（不删除 peer），并重生成 `babel.conf`。

### 输入

- 必填：`<name>`
- 必填：`--rxcost`（未提供时应交互提示）

### 行为

- 更新 DB 中该 peer 的 `babel_rxcost`。
- **重生成** `babel.conf`（确定性、幂等）。
