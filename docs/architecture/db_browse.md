# 数据库浏览（只读）

web admin 的 Database 标签页提供对全部业务表的**只读**分页浏览。这份文档定义端点形状、表白名单、脱敏规则，以及为什么这里没有通用行编辑器。

## 端点

```
GET /api/admin/db/tables
  -> [{"name": "bgp_peers", "rows": 12, "redacted": ["wg_private_key"]}, ...]

GET /api/admin/db/tables/{table}?limit=100&offset=0&node_id=<可选>
  -> {"table": "bgp_peers",
      "columns": ["id", "node_id", "peer_asn", ...],
      "rows": [{...}, ...],
      "total": 12, "limit": 100, "offset": 0,
      "redacted": ["wg_private_key"]}
```

- 鉴权：admin token（与其它 `/api/admin/*` 一致）。
- `ORDER BY rowid`——稳定，且所有可浏览表都是 rowid 表。
- `limit` ∈ [1, 500]，默认 100；`offset` ≥ 0；`total` 来自 `COUNT(*)`。
- `node_id` 过滤只对带 `node_id` 列的分区表生效。

> **不提供 CLI 命令。** `sqlite3 /var/lib/dn42ctl/dn42.sqlite3` 本来就更好用，包一层只是徒增表面积。

## 表白名单

表名走**模块级白名单 dict**，**绝不**从 `sqlite_master` 派生——URL 永远不能命名一张表。未命中直接 404。

拼 SQL 时只使用**白名单 dict 的 key**，永不使用请求里的字符串。新增表时必须在白名单里显式分类，这是有意的摩擦：让"这张新表里有没有机密"成为一个必须回答的问题。

## 脱敏

### 分类规则

> **当且仅当某列存了机密、且没有任何现有 admin 路由已经明文返回它时，才脱敏。**

| 列 | 脱敏 | 理由 |
|---|---|---|
| `bgp_peers.wg_private_key` | 是 | WireGuard 私钥 |
| `ibgp_peers.wg_private_key` | 是 | 同上 |
| `managed_nodes.api_token_hash` | 是 | argon2id hash |
| `config_revisions.payload_json` | 是 | **存的是完整 desired-state 快照，内含每一个 `wg_private_key`。** 现有的 `_revision_to_dict` 刻意从不返回它 |
| `config_proposals.payload_json` | 否 | 已被 `GET /api/admin/nodes/{id}/proposals` 全量返回 |
| `node_reports.payload_json` | 否 | 已被 `GET /api/admin/nodes/{id}/reports` 全量返回 |

`config_revisions.payload_json` 是这里最容易漏、后果也最严重的一条：漏了就等于把全网 WireGuard 私钥挂在 web 上。它替换成 `"<payload: N bytes>"`，保留体积信息但不泄露内容。

后两个 payload 列不脱敏，是因为在已有明文路由的前提下再在这里遮一层只是自欺。要收紧就得连同那两条路由一起改，那是另一件事。

### 表示形式

- 非 NULL → `"***"`
- NULL → `null`

**绝不给前缀**（`hash[:8]…` 之类）：argon2 前缀会泄露参数，WireGuard 私钥前缀是实打实的密钥空间缩减。

保留 NULL / 非 NULL 的区分是有意的——那正是 Nodes 页已经在用的 `has_token` 语义，管理员需要知道"有没有签发过 token"，但不需要知道 hash 本身。

## 为什么没有通用行编辑器

> 浏览器是**只读**的。任何权威写入都必须经过 service 层。

因为**变更通知必须与业务写入同事务发射**（`db.emit_sync_event`）。一个通用的 `UPDATE` 端点会绕过这条不变量：行改了、`sync_events` 没写、watcher 收不到、节点永远拿着旧配置，而且**没有任何告警**。

这是设计约束，不是待办事项。需要改数据就去对应实体的专用表单/端点，它们带业务校验，也保证发射事件。
