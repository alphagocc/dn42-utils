# 数据库（SQLite）设计

## 目标

- 所有状态写入 SQLite，便于多端/多节点集中管理。
- 以 `node_id`（UUIDv4）区分节点，所有业务表均带 `node_id` 字段分区。
- 保持结构可扩展（未来可迁移到 Cloudflare D1 或其它存储）。

## 迁移机制

- 使用 `schema_migrations(version)` 记录迁移版本。
- 启动/初始化时应自动执行迁移，保证旧库可直接升级。

## Schema（当前版本 v5）

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

---

## v5：多节点中心化同步表

中心化 hub-spoke 同步引入 4 张新表。架构与流程详见
`docs/architecture/sync_hub_spoke.md`。

### 5) managed_nodes

```sql
CREATE TABLE managed_nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_token_hash TEXT,
    write_policy TEXT NOT NULL DEFAULT
        '{"peer_add":"review","peer_modify":"review","peer_delete":"review","report":"auto"}',
    enabled INTEGER NOT NULL DEFAULT 1,
    is_self INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);
```

- `api_token_hash`：argon2id hash；`NULL` 表示尚未签发 node token。
- `write_policy`：JSON 字符串，按 4 类动作分别配置：
  - `peer_add` ∈ {`review`, `auto_accept`}：节点 push 新增 peer 时的处理。
  - `peer_modify` / `peer_delete` ∈ {`review`}：修改 / 删除**始终** review，schema 不接受 `auto_accept`（防止节点被入侵后篡改/抹除权威记录）。
  - `report` ∈ {`auto`, `review`}：节点上报状态进 `node_reports` 是否需要管理员审核。注意 report 永远不直接改业务表，`auto` 仅免去入队审核步骤。
- `is_self = 1`：标记为中心主机自身（self 节点）。仅用于 `dn42ctl node list` 显示 `[self]` 与 `dn42ctl node remove` 默认拒绝。
- `last_seen_at`：最近一次该节点的 pull / push / report 时间，由 server 在请求处理时更新。

### 6) config_proposals

```sql
CREATE TABLE config_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    source TEXT NOT NULL,                 -- 'push' | 'scan'
    kind TEXT NOT NULL,                   -- 'peer_add' | 'peer_modify' | 'peer_delete'
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'accepted' | 'rejected'
    received_at TEXT NOT NULL,
    decided_at TEXT,
    message TEXT,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX idx_config_proposals_node_status ON config_proposals(node_id, status);
```

- 节点 push 或 scan 推送的配置变更先落到这里，等待管理员审核（或 `auto_accept` 下立即流转）。
- `kind` 由 server 比对当前权威表自动判定。
- `message` 用于记录 reject 原因或自动审核时的校验错误。

### 7) node_reports

```sql
CREATE TABLE node_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- 'apply_result' | 'scan_result' | 'live_status' | 'error'
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    imported_at TEXT,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX idx_node_reports_node_kind ON node_reports(node_id, kind, received_at);
```

- 仅事实陈述。`imported_at` 仅在管理员显式 `dn42ctl node import-report` 后填充（目前只对 `scan_result` 有意义）。

### 8) config_revisions

```sql
CREATE TABLE config_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    revision TEXT NOT NULL,               -- 形如 '2026-05-18T10:00:00Z-001'
    generated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(node_id, revision),
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX idx_config_revisions_node_time ON config_revisions(node_id, generated_at);
```

- 每次生成 desired state 时写一条快照，供 `dn42ctl node rollback` 用。
- 保留上限由应用层定时清理（默认 50 条 / 节点）。schema 不强制。

### 设计取舍

- `write_policy` 选 JSON 而非新增策略子表：字段少、读多写少、按节点单值。
- `config_revisions` 第一阶段就建表，但写入与回滚实现在阶段 5。schema 一次到位避免再加迁移。
- `is_self` 不放索引：值集小 + 只在 CLI 显示时按 PK 已知 node_id 查询。
