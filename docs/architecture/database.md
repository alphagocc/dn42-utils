# 数据库（SQLite）设计

## 目标

- 所有状态写入 SQLite，便于多端/多节点集中管理。
- 以 `node_id`（UUIDv4）区分节点，所有业务表均带 `node_id` 字段分区。
- 保持结构可扩展（未来可迁移到 Cloudflare D1 或其它存储）。

## 迁移机制

- 使用 `schema_migrations(version)` 记录迁移版本。
- 启动/初始化时应自动执行迁移，保证旧库可直接升级。

## Schema（v2）

### 1) schema_migrations

- `schema_migrations(version)`：迁移版本表。

### 2) nodes

- `nodes(node_id, created_at, updated_at)`：节点表。

### 3) bgp_peers

外部 BGP peer（wireguard 隧道 + bird peers + 网络后端配置），字段（节选）：

- `node_id`
- `peer_asn`、`ifname`
- `wg_private_key`、`wg_public_key`
- `peer_public_key`、`endpoint`
- `local_lla`、`peer_lla`
- `listen_port`（允许为 0 表示未设置）
- `allowed_ips_json`、`net_backend`
- `created_at`、`updated_at`

约束：

- `(node_id, peer_asn)` 唯一
- `(node_id, ifname)` 唯一

### 4) ibgp_peers

内网 iBGP peer（wireguard 隧道 + bird peers + babel 互联），字段与 `bgp_peers` 类似，额外包含：

- `name`
- `babel_rxcost`（生成 `babel.conf` 时写入对应 `interface` 段的 `rxcost`）

约束：

- `(node_id, name)` 唯一
- `(node_id, ifname)` 唯一

## 安全性

- SQLite 会保存 WireGuard 私钥（用于多端/未来集中管理），请确保数据库文件权限与备份策略。
- NetworkManager 连接文件与相关配置文件目标权限应尽力设置为 0600。
