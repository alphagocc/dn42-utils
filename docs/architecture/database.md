# 数据库（SQLite）设计

## 目标

- 所有状态写入 SQLite，便于多端/多节点集中管理。
- 以 `node_id`（UUIDv4）区分节点，所有业务表均带 `node_id` 字段分区。
- 保持结构可扩展（未来可迁移到 Cloudflare D1 或其他存储）。

## 迁移机制

- 使用 `schema_migrations(version)` 记录迁移版本。
- 启动/初始化时应自动执行迁移，保证旧库可直接升级。
- 迁移是 `(version, step)` 列表，逐版本执行 + commit。`step` 可以是 **SQL 字符串**（使用 `executescript` 执行，SQL 必须**幂等**：`IF NOT EXISTS` / 条件 UPDATE），也可以是 **`Callable[[sqlite3.Connection], None]`**。
- **`ALTER TABLE ... ADD COLUMN` 必须使用 callable 分支。** SQLite 没有 `ADD COLUMN IF NOT EXISTS`，而 `executescript` 在执行前**隐式 COMMIT**、语句不在调用方事务内：脚本中途失败会留下"前几列已提交、版本号没写入、`rollback()` 也对它们无效"的状态，重新运行会有 duplicate column，导致**库永久卡死**。callable 分支在连接的隐式事务内执行，与 `schema_migrations` 插入真正原子。用 `migrations.ensure_column()`（先查 `PRAGMA table_info` 再决定是否 ALTER）。
- **版本号不连续是有意的。** v1 是合并后的建表脚本，v2–v7 已在 2026-05-31 的清理中并入 v1；v8 是 `nm` → `networkd` 的数据订正（编号跳到 8 是为了避开生产库里已应用的旧 v2）。**当前最大版本是 v13**（`is_self` 唯一索引），新迁移从 v14 开始。

## DB 与配置文件的写入顺序

**两个方向都以 DB 为先。** 创建时先提交行、再渲染文件；删除时先删行、再删文件。

理由是 DB 是唯一权威，文件是派生物：任何一步失败之后，剩下的状态都应当是"DB 是对的，
文件可能落后"这一种。相反的残留方向无法自动重建一致状态——没有任何东西会去读文件
再往 DB 里补记录。

删除方向此前是先删文件再删行，于是文件删完、删行失败时会留下一条 DB 记录，`genconf
--all` 下次就把文件重新生成出来，peer 悄悄复活。

**改成 DB 优先之后，文件删除必须是尽力而为、不中止。** 先删行再让文件删除抛异常的话，
DB 里已经没有这条 peer，而 `bird.conf` 的 `include "<peers_dir>/*";` 仍在加载那个文件
——会话还活着；再执行一次 `del` 只会得到"该 peer 不存在"，工具救不回来。所以删不掉的
文件进 `DeleteResult.failed_files` 报给用户，命令本身照常成功。

> 只有 spoke 侧的 `node apply` 会主动清扫陈旧文件（`_collect_stale`）。hub 上的
> `genconf --all` 只重建、不删除，所以孤儿文件会一直留在磁盘上。

## 事务准则

**每一条离开 DB 层的异常路径都必须先 `rollback()`。**

DB 层的写方法长成 `try: execute(...) → 检查 rowcount → emit → commit / except sqlite3.Error: rollback + raise`。
问题出在中间那步：`if cur.rowcount == 0: raise DatabaseError(...)` 抛的是**自定义异常**，
不是 `sqlite3.Error`，因此绕过了那个 `except`，也就绕过了 `rollback()`。

而 0 行匹配的 `UPDATE` 依然会拿到 `RESERVED` 锁——Python 的 sqlite3 是按语句类型
开事务的，跟改了几行无关。于是连接停在一个只读但已开启的写事务里，另一个连接的写入
会先卡满 `busy_timeout`（5 秒）再报 `database is locked`，比立即失败更难排查。

连接不会被及时回收：异常的 traceback 引用着抛出它的栈帧，栈帧引用着 `Database`，
构成引用环，只有分代 GC 能收；调用方若把异常留着（`raise HTTPException(...) from exc`、
`raise typer.Exit(1) from exc`），锁会一直持有到那条异常链本身被回收。

因此有两条规则：

1. DB 层：`raise DatabaseError` 之前显式 `rollback()`。
2. service 层：拿到 `Database` 就用 `try/finally: db.close()`——`close()` 会隐式回滚，
   把窗口压到零。这条对**所有**持有连接的路径成立，不限于 peer 增删改：`open_db_and_ensure_node`
   自身的失败分支、`scan` 与 `init_sys` 里那些从头执行到尾的长函数同样适用。

这条路径要命中需要跨进程 TOCTOU（另一个进程在 `SELECT` 与 `UPDATE` 之间删掉了行），
罕见但不是不可能——hub 上 CLI 与 server 本来就在并发写同一个库文件。

## 连接 PRAGMA

每个连接在打开后设置：

- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = 5000`：hub 上 server 进程与 CLI 进程会并发写同一个库文件（管理员执行 `dn42ctl bgp peer add` 的同时 server 的 `sync_events` watcher 在读）。没有 busy_timeout 时，撞锁会立刻抛 `database is locked`，失去等待重试的机会。

> **不启用 WAL。** WAL 会在库文件旁生成 `-wal` / `-shm`，owner 是创建它们的进程；hub 上 `sudo dn42ctl ...`（root）创建之后，以 `dn42ctl` 用户运行的 server 会被锁在外面。现有的写入都很短，`busy_timeout` 已经够用。

## Schema（single consolidated migration）

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
- `peer_ip`（Bird neighbor 地址，网内 IPv6）
- `has_wg`（是否创建 WireGuard 隧道，默认 1）
- `babel_rxcost`（生成 `babel.conf` 时写入对应 `interface` 段的 `rxcost`，默认 20）
- `babel_type`（`wired` / `wireless` / `tunnel`，默认 `tunnel`）
- `remote_node_id`（v10 新增，可空）：这条 peer 记录所代表的受管节点。用于把节点地址变更传播到 mesh，详见 [`node_addressing.md`](node_addressing.md)。**刻意不加外键**——删除节点 A 不能级联删掉节点 B 指向 A 的 peer 行（那是 B 的配置）；悬空引用无害，传播时跳过并告警。索引 `idx_ibgp_peers_remote(remote_node_id)`。

约束：

- `(node_id, name)` 唯一
- `(node_id, ifname)` 唯一

## 安全性

- SQLite 会保存 WireGuard 私钥（用于多端/未来集中管理），请确保数据库文件权限与备份策略。
- NetworkManager 连接文件与相关配置文件目标权限应尽力设置为 0600。

---

## 多节点中心化同步表

中心化 hub-spoke 同步引入 5 张新表。架构与流程详见 `docs/architecture/sync_hub_spoke.md`，WebSocket 推送与 `sync_events` 的用法详见 `docs/architecture/sync_ws_protocol.md`。

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
    -- v10 新增,三列全部可空且无 DEFAULT
    endpoint_host TEXT,
    own_ipv6 TEXT,
    router_id TEXT,
    -- v14 新增
    auto_peer INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);
```

- `auto_peer`：该节点是否出现在公共 auto-peer 页面上。默认 0，新增与升级出来的节点都要运维显式开放；公开列表与提交校验都取 `auto_peer=1 AND enabled=1`，语义见 [`auto_peer.md`](auto_peer.md)。
- `endpoint_host` / `own_ipv6` / `router_id`：节点地址，**`NULL` 表示该字段不由中心管理**（不下发，节点本地值原样保留）。语义、传播规则与下发机制见 [`node_addressing.md`](node_addressing.md)。`endpoint_host` 只存主机、不含端口——端口是对端每条隧道的 `listen_port`，存不进节点级字段。
- `api_token_hash`：`sha256$<hex>`；`NULL` 表示尚未签发 node token。v11 把存量的旧格式 hash 全部置 NULL，强制全部重签，操作步骤见 [`deployment.md`](deployment.md)。
- `write_policy`：JSON 字符串，按 4 类动作分别配置：
  - `peer_add` ∈ {`review`, `auto_accept`}：节点 push 新增 peer 时的处理。
  - `peer_modify` / `peer_delete` ∈ {`review`}：修改 / 删除**始终** review，schema 不接受 `auto_accept`（防止节点被入侵后篡改/抹除权威记录）。
  - `report` ∈ {`auto`, `review`}：节点上报状态进 `node_reports` 是否需要管理员审核。注意 report 永远不直接改业务表，`auto` 仅免去入队审核步骤。
- `is_self = 1`：标记为中心主机自身（self 节点）。**全表至多一行，由 schema 强制**——v13 的 partial unique index `idx_managed_nodes_single_self ON managed_nodes(is_self) WHERE is_self = 1` 把这条不变量落到数据库层；`upsert_self` 先把其他行清零再写入，`get_self()` 带确定性排序兜底，v12 迁移清理存量的多 self 行（保留 `updated_at` 最新的一行，其余降级为普通节点）。只靠应用路径不够：直接写 SQL、未来新增的写入点、以及迁移之后又被手工改坏的库，都会绕过它，而这一列决定每一次 admin 写入落到哪个分区。
  - 这一列不只是显示用：`api.py` 的 `_resolve_target_node` 用它决定每一次 admin 写入落到哪个分区（见 [`rest_api.md`](rest_api.md)），`node adopt-self` 用它确定搬迁目标。出现两行时 `get_self()` 会返回其中一行且没有任何排序保证，这两处会一起指向错误的分区。
  - 出现两行的现实路径是 `/var/lib/dn42ctl/self_node_id` 丢失后重新生成（容器没挂持久卷、磁盘恢复、误删）：`upsert_self` 写入新 id 却不动旧行。旧行降级保留，是因为旧分区里的 peer 全都还在，删掉等于丢配置；降级后它作为普通受管节点出现在 `node list` 里，可以用 `node adopt-self` 把 peer 搬过来。
- `last_seen_at`：最近一次该节点的 pull / push / report 时间，由 server 在请求处理时更新。WS 长连接下由节点每 60 秒发起的 `ping` 触发（每连接节流到最多 60 秒一次，避免心跳放大写入）。

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

### 9) node_desired_pin

```sql
CREATE TABLE node_desired_pin (
    node_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    pinned_at TEXT NOT NULL,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);
```

- 每个节点至多一条 pin 记录，表示该节点的 desired state 锁定到指定 revision。
- `dn42ctl node rollback` 设置 pin；`dn42ctl node unpin` 删除 pin，恢复跟随最新。
- `RevisionStore.trim()` 会保护被 pin 的 revision 不被清理。

### 10) sync_events

```sql
CREATE TABLE sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- 'desired' | 'access_revoked'
    created_at TEXT NOT NULL
);
CREATE INDEX idx_sync_events_node ON sync_events(node_id, id);
```

变更通知队列。`dn42ctl serve` 的后台 watcher 每秒轮询这张表，把 `desired` 事件转成对应节点的 WebSocket 推送，把 `access_revoked` 事件转成断开该节点的连接。存在的理由是 CLI 命令是**独立进程**写同一个库文件，server 进程内存里的连接注册表收不到通知。

写入点全部在 DB 层，插进业务写入**同一个事务**（各方法在自己 `commit()` 之前发射），所以不存在"peer 写了但事件没记"的崩溃窗口：

| 文件 | 方法 | kind |
|------|------|------|
| `db.py` | `insert_bgp_peer` / `update_bgp_peer` / `insert_ibgp_peer` / `update_ibgp_peer` / `_delete_peer` | `desired` |
| `db_managed.py` | `RevisionStore.pin` / `unpin` | `desired` |
| `db_managed.py` | `ManagedNodeStore.set_addresses` / `apply_address_update` | `desired`（被改地址的节点 + 每个被传播到的节点，去重） |
| `db_managed.py` | `ManagedNodeStore.set_token_hash` / `delete` / `set_enabled(False)` | `access_revoked` |

> **`set_enabled(False)` 必须发 `access_revoked`。** `authenticate` 虽然过滤 `enabled=1`，但 WebSocket 握手只验一次 token，之后整条连接吃缓存 principal——不发事件的话，禁用一个节点不会影响它**已经建立**的连接，该连接将无限期保持授权。

两个刻意的 schema 选择：

- **`AUTOINCREMENT` 是必需的。** 裸 `INTEGER PRIMARY KEY` 在最大行被删除后会复用 rowid，而裁剪会常规性地删行。rowid 一旦被复用，watcher 游标就会静默倒退并丢事件。
- **不加 FOREIGN KEY。** `remove_node` 必须发 `access_revoked`，那行得在节点被删除后存活。

裁剪：发射时按 `id % SYNC_EVENTS_TRIM_EVERY == 0`（256）触发 `DELETE FROM sync_events WHERE id <= ? - SYNC_EVENTS_KEEP`（1000），常量在 `constants.py`。watcher 游标启动时初始化为 `MAX(id)` 且只前进，所以裁剪在任何游标位置下都安全。

> spoke 的本地库也会因本机 `bgp peer add` 累积 `sync_events`。那里没人读它，裁剪保证有界，惰性无害。

### 设计取舍

- `write_policy` 采用 JSON 字段存储：字段少、读多写少、按节点单值，无需独立的策略子表。
- `sync_events` 采用轮询小表方案，取代"server 定时重算内容哈希"与"CLI kick loopback 端点"两个候选：写入与业务变更同事务、不要求 CLI 能访问 server、漏埋点不会静默失效。轮询没有消失，但它从跨网络每 10 分钟一次变成了 hub 本机每秒一次的带索引 SELECT。
- `config_revisions` 第一阶段就建表，但写入与回滚实现在阶段 5。schema 一次到位避免再加迁移。
- `is_self` 不放索引：全表至多一行为 1，扫描代价与索引查找相当。
