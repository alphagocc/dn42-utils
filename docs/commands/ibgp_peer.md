# 命令：ibgp peer

## `dn42ctl ibgp peer`

用途：创建内网 iBGP peer（wireguard 隧道 + bird peers + babel 互联）。

### 输入

- 必填：`--name`、`--pubkey`、`--endpoint`、`--peer-lla`、`--net`。
- 可选：`--listen-port`
  - `0` 表示不设置
  - 留空则自动选择未占用端口（避免与当前节点已有端口冲突）

### 派生规则

- `ifname`：`wg_<sanitize(name)>`（长度不得超过 15）
- `ListenPort`：默认从高端口随机选择且避免冲突；也允许通过 `--listen-port` 覆盖

### 输出

- Bird iBGP peer conf：写入 `bird_peers_dir/ibgp_<name>.conf`。
- 写入 networkd 或 NetworkManager 的 wireguard 配置文件。
- **重生成** `babel.conf`：从数据库读取该节点所有 iBGP peer 的接口列表，确定性、幂等地生成。
