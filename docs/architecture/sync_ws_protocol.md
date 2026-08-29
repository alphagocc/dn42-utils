# 节点同步 WebSocket 协议

常驻节点 agent 与中心 hub 之间的长连接同步协议。取代原先由 `dn42ctl-node-once.timer` 每 10 分钟驱动一次的 HTTP 轮询。

- 角色与数据所有权：`docs/architecture/sync_hub_spoke.md`
- HTTP 路由与鉴权：`docs/architecture/rest_api.md`
- 表结构：`docs/architecture/database.md`
- systemd unit / nginx 反代：`docs/architecture/deployment.md`
- CLI 参数：`docs/commands/node.md`

## 为什么改

轮询模型有三个固有问题：

- **收敛延迟最坏 10 分钟。** 管理员在 hub 上 `bgp peer add` 之后要等下一个 timer 周期。
- **稳态下绝大多数请求是空转。** 配置没变时每次 pull 仍要跑一次 `build_desired_state`（含一次 `config_revisions` 写入）+ 一次全表扫描鉴权。
- **中心无法主动下发。** rollback、token 轮换、节点禁用都只能等节点自己回来问。

WebSocket 长连接把收敛延迟降到 ≤1s，并消除稳态空转。

## 传输通道划分

| 通道 | 使用者 | 承载 |
|------|--------|------|
| **WebSocket** `/api/v1/nodes/{node_id}/ws` | 常驻 `dn42ctl node agent` | desired 下发、proposal / report 上报、心跳 |
| **HTTP** `/api/v1/nodes/{node_id}/{desired,proposals,reports,status}` | 一次性 CLI 命令（`node pull` / `apply` / `once` / `push` / `report` / `status`） | 人工排障 |

两条通道共用同一套 Bearer token 鉴权与同一套 service 层，语义完全等价。一次性 CLI 命令是独立进程，用不了常驻 agent 持有的那条连接，所以 HTTP 路由**保留**。

## 信封

```json
{
  "v": 1,
  "type": "proposal_submit",
  "id": "<uuid4 hex>",
  "re": null,
  "ts": "2026-07-29T12:00:00+00:00",
  "payload": {}
}
```

| 字段 | 必需 | 含义 |
|------|------|------|
| `v` | 是 | 协议版本，恒为 `1` |
| `type` | 是 | 消息类型，见下方目录 |
| `id` | 是 | uuid4 hex，**本条消息自己的** id |
| `re` | 否 | 被回应消息的 `id`。仅出现在 `ack` / `error` / `pong` / 回应 `desired_request` 的 `desired_push` 上 |
| `ts` | 是 | RFC3339 UTC，仅供参考，不参与任何判定 |
| `payload` | 是 | 对象，可以是 `{}` |

文本帧、JSON、UTF-8。双向帧上限 **8 MiB**（低于 uvicorn 默认的 16 MiB，使超限在应用层暴露）。

> **`v` 的作用是 fail-fast 校验。** 本项目沿用"所有节点运行统一版本"的既有约定（见 `docs/spec.md`），不做向后兼容。`v` 是一道 fail-fast 围栏：收到不匹配的 `v` 就回 `error{code:"version_mismatch"}` 然后 close `4008`，让版本歪斜以显式错误暴露、在日志里一眼可见，避免字段缺省值造成静默错乱。**请勿**把它当作兼容钩子使用。

## 鉴权：握手时的 Bearer header

WS 握手请求携带 `Authorization: Bearer <node token>`，与 HTTP 路由完全相同的 token。采用 header 传递的理由：Python 客户端能设 header；nginx 原样透传；可直接复用 `ManagedNodeStore.authenticate`；token 不会落进可能被日志记录的 JSON 帧。

（浏览器的 `WebSocket` API 无法设置 header，但这不构成约束：admin Web UI 不使用本通道，见 `docs/architecture/web_ui.md`。）

两个实现上必须避开的问题：

- **不要在 WS 路由上用 `Depends(_resolve_principal)`。** 它靠 duck-typing 能跑通（`HTTPBearer.__call__` 在此收到的是 `WebSocket` 类型），但失败时抛 `HTTPException`，Starlette 在 WebSocket 连接上渲染不出来。应当手工读 `websocket.headers.get("authorization")`。
- **先 `accept()` 再 `close()`。** 按 ASGI 规范，在 `accept()` 之前 `close()` 会让握手直接回 HTTP 403，客户端只能看到一个 `InvalidStatus`，无从得知失败原因。正确顺序是 `accept()` → 鉴权 → 失败时先发 `error` 帧、再 `close(code)`。握手前窗口要加硬超时（`4408`），防止未鉴权连接长期占用。

**token 校验每连接只做一次。** `ManagedNodeStore.authenticate` 是对所有 enabled 节点线性扫描 + 逐行比对。握手时验一次，把结果 `Principal` 缓存到连接对象上，之后每一帧的鉴权纯粹读缓存，零 DB。

## 消息目录

### node → hub

| type | payload | 回应 |
|------|---------|------|
| `hello` | `{node_id, agent_version, cached_revision: str\|null}` | `hello_ack` |
| `desired_request` | `{reason: "reconcile"\|"resync"\|"manual"}` | `desired_push`（**无条件**，绕过去重） |
| `proposal_submit` | `{source, kind, payload}` | `ack{proposal_id, status}` 或 `error` |
| `report_submit` | `{kind, payload}` | `ack{report_id, received_at}` 或 `error` |
| `ping` | `{}` | `pong` |

### hub → node

| type | payload |
|------|---------|
| `hello_ack` | `{node_id, server_version, revision, in_sync: bool}` |
| `desired_push` | `{revision, generated_at, desired: {…完整 desired state…}}` |
| `ack` | 随请求而定，`re` 指向请求 |
| `error` | `{code, message}`，回应请求时带 `re` |
| `pong` | `{}`，带 `re` |
| `shutdown` | `{reason}`，紧接着 close `4000` |

`desired_push.desired` 与 `GET /api/v1/nodes/{id}/desired` 的响应体**逐字节一致**（都是 `DesiredState.to_dict()`），所以 spoke 的缓存写入与 `apply()` 路径两条通道通用。schema 见 `docs/architecture/sync_hub_spoke.md`。

`report_submit` 的 `apply_result` payload 形状与 `dn42ctl node once` 上报的完全一致（`{ok, revision, create, update, unchanged, delete}`），hub 侧 report 消费方无需区分来源。

### `error.code` 枚举

稳定 ASCII 标识，供程序判定；`message` 按项目惯例是简体中文，供人阅读。

`unauthorized` · `forbidden` · `not_found` · `bad_envelope` · `unknown_type` · `payload_invalid` · `service_error` · `revoked` · `too_many_connections` · `version_mismatch` · `internal`

收到 `error` 时连接**保持打开**（除非紧跟一个 close 帧）。单条消息失败不该拖垮整条连接。

## 关闭码

使用 RFC 6455 私有段 4000–4999。

| code | 含义 | agent 反应 |
|------|------|-----------|
| `1000` | 正常关闭 | 正常退避 |
| `1011` | hub 内部错误 | 正常退避 |
| `4000` | hub 正在关闭 | **短**退避（hub 多半正在重启） |
| `4003` | 访问被撤销（token 轮换 / 节点禁用） | auth 退避 + 显著日志 |
| `4004` | 节点已被删除 | auth 退避 + 显著日志 |
| `4008` | 协议版本不匹配 | auth 退避 + 显著日志 |
| `4009` | 该节点并发连接数超限 | 正常退避 |
| `4401` | 鉴权失败（缺 token / token 无效） | auth 退避 |
| `4403` | token 有效但与 path 中的 `node_id` 不匹配 | auth 退避 |
| `4404` | managed node 不存在 | auth 退避 |
| `4408` | 握手 / hello 超时 | 正常退避 |

`4401` / `4403` / `4404` 与 HTTP 路由的 401 / 403 / 404 语义一一对应。

## 连接生命周期

```text
DISCONNECTED ──(启动时先用本地 cache 跑一次 apply)──► HANDSHAKING ──► AUTHENTICATING
     ▲                                                                     │ 鉴权仅此一次 
     │                                                                     ▼
  BACKOFF ◄─── 传输错误 / close ─── STEADY ◄─ INITIAL_SYNC ◄─ HELLO (15s 超时)
                                      │
                                      │  reader ∥ heartbeat(60s) ∥ reconcile(900s)
                                      └─ desired_push → 写缓存 → apply → report_submit
```

- **HANDSHAKING**：TCP / TLS / HTTP Upgrade。任何失败 → 正常退避。
- **AUTHENTICATING**：hub `accept()` 后读 header，验一次 token，校验 path 中的 `node_id`。
- **HELLO**：node 发 `hello{cached_revision}`，hub 回 `hello_ack`。15 秒不发 hello → close `4408`。
- **INITIAL_SYNC**：`hello_ack.in_sync == false` 时 hub 立即推 `desired_push`；为 `true` 时不推。无论哪种情况，agent 都会用本地缓存跑一次 `apply()`（幂等）。
- **STEADY**：三个并发任务：reader 分发入站消息、heartbeat 每 60s 发 `ping`、reconcile 每 900s 发 `desired_request{reason:"reconcile"}`。
- **BACKOFF**：见下方退避策略。

### 开机收敛保证

删掉 `node-once.timer` 的同时也删掉了它的 `OnBootSec=2min`。如果 spoke 重启时 hub 恰好不可达，就再没有任何东西会去渲染 `/etc/bird`。

因此 **agent 必须在尝试第一次连接之前，先从本地缓存跑一次 `apply()`**（best-effort，失败只记日志）。这是"无 timer 兜底"这个设计能够成立的前提。

### 退避策略

**full jitter**：`delay = uniform(0, min(cap, initial * 2 ** attempt))`。

采用 full jitter 的原因在于，主要失效模式就是 **hub 重启后全队同步重连**：N 个节点同时握手、各自触发一次 `build_desired_state`，尖峰全压在同一瞬间。full jitter 把它摊得最平。

鉴权类致命关闭（`4003` / `4004` / `4008` / `4401` / `4403` / `4404`）改用固定的 `auth_retry_seconds`（默认 300s）：指数爬坡会让一个过期 token 演变成对 hub 的 1 秒间隔重连风暴。

**agent 每轮重连都重读 `node.toml`。** 这让 token 轮换后无需 `systemctl restart`：`dn42ctl node token rotate` 会重写 self 节点的 `node.toml`，远程节点由管理员更新文件即可，下一轮重连自动生效。

## Hub 侧：变更检测

### 为什么需要 `sync_events` 表

`dn42ctl bgp peer add` 等 CLI 命令是**独立进程**写同一个 SQLite 文件。server 进程内存里的连接注册表收不到任何通知。三种候选方案：

| 方案 | 问题 |
|------|------|
| server 定时算每个节点的内容哈希 | 节点多时每轮都要全量重算 desired state |
| CLI 写完后 kick 一个 loopback 端点 | 需要 CLI 能访问 server（可能没起）、需要额外鉴权、漏一处调用就静默失效 |
| **`sync_events` 表 + server 轮询** | 采用。写入与业务变更同事务，轮询的是一张只有整型主键的小表 |

轮询没有消失，但它从**跨网络、每 10 分钟一次**变成了 **hub 本机、每秒一次的一条带索引的 SELECT**。

### 表结构

```sql
CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_events_node ON sync_events(node_id, id);
```

`kind ∈ {"desired", "access_revoked"}`。

两个刻意的选择：

- **`AUTOINCREMENT` 是必需的。** 裸 `INTEGER PRIMARY KEY` 在最大行被删除后会**复用 rowid**，而裁剪操作会常规性地删行。一旦 rowid 被复用，watcher 的游标就会静默倒退并丢事件。`AUTOINCREMENT` 保证 id 单调、永不复用。
- **不加 FOREIGN KEY。** `remove_node` 必须发 `access_revoked`，而那一行得在节点被删除之后**存活**下来。没有 FK 也就没有级联删除的意外。表的增长由裁剪控制。

### 发射点（9 处，全在 DB 层）

`db.py` 是天然的唯一收敛点：全部权威写入只经过 5 个方法，且它们都已经知道 `node_id`。在 service 层埋点会漏掉 `services/scan.py`，它绕过 service 直接调 `db.insert_*_peer`。更关键的是，在 DB 层可以把事件插进**调用方已打开的同一个事务**（这些方法各自 `commit()`，在 commit 之前插入即可），不存在"peer 写了但事件没记"的崩溃窗口；service 层做不到这一点。

| 文件 | 方法 | kind |
|------|------|------|
| `db.py` | `insert_bgp_peer` / `update_bgp_peer` / `insert_ibgp_peer` / `update_ibgp_peer` / `_delete_peer` | `desired` |
| `db_managed.py` | `RevisionStore.pin` / `RevisionStore.unpin` | `desired` |
| `db_managed.py` | `ManagedNodeStore.set_token_hash` / `ManagedNodeStore.delete` | `access_revoked` |

`RevisionStore.pin`/`unpin` 是唯一逃出 `db.py` 的写入点：rollback 改的是 `node_desired_pin`，不碰 peer 表。

> `update_*_peer` 在 UPDATE 实际没改动任何行时（字段值与原值相同）也会发事件。这是**无害**的：hub 的内容指纹比对会吞掉它，不会发出任何帧，属于预期行为。

### 裁剪

在发射函数里用 `new_id % SYNC_EVENTS_TRIM_EVERY == 0` 触发 `DELETE FROM sync_events WHERE id <= ? - SYNC_EVENTS_KEEP`（`SYNC_EVENTS_KEEP = 1000`，`SYNC_EVENTS_TRIM_EVERY = 256`，均在 `constants.py`）。watcher 游标初始化为 `MAX(id)` 且只前进，所以任何游标位置下裁剪都是安全的。

> 每个 spoke 的本地库也会因本机 `bgp peer add` 累积 `sync_events`。那里没人读它，裁剪保证它有界，惰性无害。

### Watcher

挂在 FastAPI lifespan 上的后台任务：

```text
last_id = SELECT COALESCE(MAX(id), 0) FROM sync_events      # 启动时
loop:
    sleep(poll_interval)                                     # 默认 1.0s
    rows = fetch_since(last_id, limit=500)
    last_id = max(r.id for r in rows) if rows else last_id
    revoked = {r.node_id for r in rows if r.kind == "access_revoked"}
    desired = {r.node_id for r in rows if r.kind == "desired"}
    for nid in revoked:            close_node(nid, 4003)
    for nid in desired - revoked:  notify(nid)
```

- **游标初始化为 `MAX(id)`。** 启动之前的事件已经被每条连接的初始同步覆盖了（见下方 level-triggered 论证）。这同时也保证裁剪永远不可能把游标搁浅。
- 循环体整体捕获异常 → 记日志 → 继续。一次瞬时 `DatabaseError` 不能让全队的同步一起死掉。
- 轮询间隔通过 `dn42ctl serve --sync-poll-interval`（环境变量 `DN42CTL_SYNC_POLL_INTERVAL`）配置。
- 关停时对所有连接 `close(4000)`。

### level-triggered 语义

这是本设计正确性的核心论证。

初始同步读取的是**当前**完整内容，与增量重放无关。watcher 收到事件后触发的也只是一次**重新检查**，而重新检查是幂等的：hub 计算内容指纹，与该连接的 `last_pushed_hash` 比对，相同则跳过推送。

于是"事件与新连接竞态"这个经典问题不存在：事件要么已经被初始读取反映了（指纹相同 → 正确地不推），要么在其后到达（→ 推）。**更新永不丢失，连接与 watcher 之间也无需任何游标协调。**

这也是 watcher 游标可以从 `MAX(id)` 开始的原因。

### 连接期间的 token 轮换 / 删除 / 禁用

这些操作发生在**另一个进程**（`dn42ctl node token rotate <id>`）中，这正是 `sync_events` 相对于 loopback kick 端点的价值所在：kick 方案要求 CLI 能访问到 server，而这里没有这个前提。

`set_token_hash`（`rotate_token` 委托给它）与 `delete` 发 `access_revoked`，watcher 收到后对该节点 `close_node(4003)`。agent 进入 auth 退避并打显著日志；管理员更新 `node.toml` 后，下一轮重连自动恢复（agent 每轮重读配置文件）。

## Hub 侧：连接管理

### 注册表

`dict[node_id, dict[conn_id, NodeConnection]]`，由一把锁保护。

**允许同一节点多条并发连接，上限 4。** 一条半死的旧 socket 不该把健康的新连接挤掉（反之亦然）；两条连接都收推送，因为 apply 是幂等的所以无害。超过 4 条时用 `4009` 关掉最老的一条，以此阻断重连风暴造成的 fd 泄漏。

每条连接用一个 task group 起 reader 与 pusher 两个任务，reader 返回（连接断开）即取消整个 scope。

### 心跳

hub 侧复用 uvicorn 的协议级 ping（每 20 秒一次），无需另起应用层 ping 循环。应用层的 `ping`/`pong` 由**节点**每 60 秒发起，同时兼作 `touch_last_seen` 的触发点（每连接节流到最多 60 秒一次，避免心跳放大写入）。

### 阻塞调用

现有 service 层全部是同步阻塞的（`build_desired_state`、`submit_proposal`、`submit_report`、`authenticate` 等），而 WS 端点必须是 `async def`。所有这些调用一律放到线程池执行：在事件循环里直接运行时，一次 sqlite 查询就会卡住整个 hub 的所有连接。

每个调用在 worker 线程里自开自关 sqlite 连接（沿用现有 `Database.open` / `close` 模式），不跨线程共享连接。

### 避免 revision churn

`_compute_revision` 把时间戳编进了 revision 字符串，所以**内容不变时每次 `build_desired_state()` 都会写一行新的 `config_revisions` 并触发 `trim(keep_latest=50)`**。agent 的定期 reconcile 会因此把 rollback 历史窗口压缩到十几个小时。

两层防护：

1. **推送前先算指纹。** `compute_desired_fingerprint()` 读 peers **和** pin（有 pin 时对 pinned payload 取哈希），**零写入**。与连接的 `last_pushed_hash` 相同、且本次不是显式 `desired_request` 时直接跳过，零 DB 写入、零帧。
   > `build_desired_state(record_revision=False)` 无法替代它：该实现跳过 pin 查询，对已回滚的节点会给出错误答案。
2. **`build_desired_state` 内部去重。** `RevisionStore.record()` 之前先比对最新已记录 revision 的内容摘要，相同则跳过。这同时也修好了 HTTP `GET /desired` 路径。

## 已知限制与后续工作

- **单进程假设。** 连接注册表在进程内存里。当前 `dn42ctl serve` 是 `uvicorn.run(app_object)`，单进程单 worker，本来也用不了 `--workers`。即便多 worker，构造上也是对的（每个 worker 各跑一个 watcher、只推自己持有的连接），但未经测试，请勿依赖。
- **spoke 侧常驻 root 进程。** 相比原先每 10 分钟约 1 秒的 oneshot，这是纵深防御的实质性降低。量化对比与缓解措施见 `docs/architecture/deployment.md`。
