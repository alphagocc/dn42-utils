# 命令：node

`dn42ctl node` 是中心化同步的命令组。**admin 子命令**（中心主机执行）与**节点子命令**（spoke 主机执行）混在同一个 group 下，靠第二级动词区分。Typer 不会冲突。

详细架构见 `docs/architecture/sync_hub_spoke.md`，节点 agent 的同步协议见 `docs/architecture/sync_ws_protocol.md`。

---

## admin 子命令（在中心主机执行）

### `dn42ctl node add <node-id> --name <name>`

注册一个新的被管节点。

- `node-id` 必须是合法 UUIDv4。
- 不签 token，需要随后调 `node token rotate <node-id>` 才能让该节点接入。
- `write_policy` 取默认 JSON：
  ```json
  {"peer_add":"review","peer_modify":"review","peer_delete":"review","report":"auto"}
  ```

### `dn42ctl node list`

列出所有 managed_nodes。`is_self=1` 的行标记 `[self]`。

### `dn42ctl node show <node-id>`

打印单节点详情：name / is_self / enabled / write_policy / last_seen_at / 最近 N 条 revision。

### `dn42ctl node remove <node-id> [--force]`

注销节点。删除 `managed_nodes` 行，级联清空 `config_proposals` / `node_reports` / `config_revisions`。

- 若 `is_self=1`，默认拒绝并提示用 `--force`。强制删除会同时清空 `/etc/dn42ctl/node.toml` 的 `server/node_id/token` 并打 warning（下次 `dn42ctl serve` 启动会自动重新注册 self 节点）。
- 删除 `node.toml` 失败（权限不足等）不会让整条命令失败——`managed_nodes` 行已经删掉了，回滚不了。失败原因通过 `self_node_toml_error` 返回，CLI 打 warning，REST 响应带该字段。

### `dn42ctl node token rotate <node-id>`

重签 node token：

1. 生成 `secrets.token_urlsafe(32)`。
2. SHA-256 hash 写 `managed_nodes.api_token_hash`。
3. **明文 token 仅在此命令返回时打印一次**。
4. 若 `is_self=1`：同步重写中心主机的 `/etc/dn42ctl/node.toml`。

旧 token 立即失效。中心会写一条 `access_revoked` 事件，watcher 随即用关闭码 `4003` 断开该节点的 WS 连接；agent 进入 300 秒长退避，更新 `node.toml` 后自动恢复。

#### 第 4 步失败必须说出来

hub 以非 root 的 `dn42ctl` 用户运行，而 `node.toml` 是 `0600 root:root`——读写它失败是**标准部署下就会发生**的事，不是异常状态。此时第 2 步已经提交，DB 里的 hash 已经换掉，等于把 hub 自己的 agent 锁在门外。

所以第 4 步失败**不能**回滚（明文只存在于这一次响应里，回滚会把它丢掉），也**不能**沉默。`RotatedToken` 带 `self_node_toml_updated` 与 `self_node_toml_error` 两个字段，CLI 打 warning，`POST /api/admin/nodes/{id}/token` 的响应体一并返回，供 Web UI 提示管理员手工更新文件。

> 即使漏了，`dn42ctl serve` 下次启动时会发现 `node.toml` 与库中 hash 对不上并自动重签（见 [`sync_hub_spoke.md`](../architecture/sync_hub_spoke.md)）。但那要等到下一次重启，中间这段时间 hub 的配置停止更新，所以仍然要当场告警。

### `dn42ctl node policy set <node-id> [选项]`

修改 `write_policy` JSON。选项：

- `--peer-add review|auto_accept`
- `--peer-modify review`（仅接受 `review`，schema 不允许 auto）
- `--peer-delete review`（仅接受 `review`）
- `--report auto|review`

未指定的字段不变。

### `dn42ctl node set-address <node-id> [选项]`

修改节点地址并按需传播到 mesh。选项：

- `--endpoint-host HOST`：公网可达主机名/IP，**不含端口**
- `--own-ipv6 ADDR`：DN42 ULA 地址
- `--router-id IPV4`：bird `router id`
- `--clear-endpoint-host` / `--clear-own-ipv6` / `--clear-router-id`：清除该字段，交还节点本地管理
- `--no-propagate`：只改 `managed_nodes`，不改写其他节点的 `ibgp_peers` 行
- `--dry-run`：只打印将要发生的改动与告警，不写库

未指定的字段不变。传播规则、无法自动推导的情形、以及为什么端口永不自动改写，见 [`../architecture/node_addressing.md`](../architecture/node_addressing.md)。

### `dn42ctl node rename <node-id> <name>`

修改节点的显示名。等价于 `PATCH /api/admin/nodes/{node_id}` 只传 `name`，不触发地址传播。

### `dn42ctl node auto-peer <node-id> --enable|--disable`

开放或收回该节点在公共 auto-peer 页面上的入口。默认关闭，`--enable` 之后它才出现在 `GET /api/public/auto-peer/nodes` 的返回里，并接受指向自己的 peering 提案。

禁用节点（`enabled=0`）同时收回入口，无需再执行 `--disable`。语义见 [`../architecture/auto_peer.md`](../architecture/auto_peer.md)。

### `dn42ctl node mesh backfill [--dry-run]`

一次性写入 `ibgp_peers.remote_node_id`：按 `managed_nodes.own_ipv6 == ibgp_peers.peer_ip` 唯一匹配。匹配不唯一或匹配不到的行会被跳过并列出。

> 这个动作**不放进数据库迁移**：迁移时所有 `own_ipv6` 都还是 NULL，匹配不到任何行。

### `dn42ctl node adopt-self [--from <uuid>] [--dry-run]`

修复 `config.toml` 的 `node_id` 与 self 节点 id 分叉的存量部署：在一个事务里把 peer 行从失效分区重新挂到 self 节点，并对该节点发一条 `desired` 事件。

- `--from`：源分区 node_id，默认取 `config.toml` 的 `node_id`。
- 目标分区非空时**拒绝执行**——两边都有行意味着已经有人在新分区下写过配置，合并策略只能由人来定；硬搬还会撞 `UNIQUE(node_id, ifname)`。

背景见 [`../architecture/node_addressing.md`](../architecture/node_addressing.md) §9。

### `dn42ctl node proposals <node-id> [--status pending|accepted|rejected]`

列出该节点的配置提案。默认显示 `pending`。

### `dn42ctl node accept-proposal <proposal-id>`

接受提案：把 `payload_json` 喂给现有 `create_bgp_peer / modify_bgp_peer / delete_bgp_peer`（或 ibgp 对应函数）。

- service 校验失败 → proposal 保持 `pending`，命令返回错误。
- 成功 → proposal 标记 `accepted`，`decided_at` 写当前时间。

### `dn42ctl node reject-proposal <proposal-id> --reason "..."`

标记 proposal 为 `rejected`，`message` 字段写 reason。不可省 reason。

### `dn42ctl node reports <node-id> [--kind apply_result|scan_result|live_status|error]`

列出该节点的上报。默认显示最近 50 条；`--kind` 过滤。

### `dn42ctl node import-report <report-id>`

仅对 `kind=scan_result` 的 report 有意义：把扫描出的 peer 转换成 `create_bgp_peer / create_ibgp_peer` 调用。

- 与节点直接 push proposal 等价；提供这个命令是为了管理员可以从历史 report 里挑选导入。
- 成功后 `imported_at` 字段被填充。
- 返回计数包含 `malformed`：payload 的 `bgp_peers` / `ibgp_peers` 数组里不是对象的条目会被跳过并计入这一项。
  这些条目**不会**让整次导入失败——它们本来就没法转成 peer——但也不能不作声：`imported_at` 一旦写上，
  这份 report 就再也不能重导（`已被导入过` 会直接拒绝），丢掉的条目将没有任何补救途径。CLI 对
  `malformed > 0` 打 warning，REST 响应带该字段。要真正找回那些 peer，只能让节点重新 `node scan` 出一份新
  report。

### `dn42ctl node revisions <node-id>`（阶段 5）

列出该节点的 desired state 历史快照，按 `generated_at` 倒序。

### `dn42ctl node rollback <node-id> --to <revision>`（阶段 5）

把该节点的"当前期望"指向指定 revision。设置 `node_desired_pin` 后，中心会写一条 `desired` 事件，watcher 随即把回滚后的 desired state 推给该节点（≤1 秒生效），无需等节点自己来拉。

---

## 节点子命令（在 spoke 主机执行）

> **稳态同步使用 `dn42ctl node agent`**（常驻，WebSocket 长连接）。下面的 `pull` / `apply` / `once` / `push` / `report` / `status` 都是**一次性命令**，使用 HTTP 路由，保留用于人工排障。两条通道共用同一套 token 与 service 层。

### `dn42ctl node agent [--node-config-path PATH]`

**常驻同步 agent**，由 `dn42ctl-node-agent.service` 拉起。持有一条到中心的 WebSocket 长连接，承载 desired 下发、proposal / report 上报与心跳。协议详见 `docs/architecture/sync_ws_protocol.md`。

行为：

1. **启动时先用本地缓存执行一次 `apply()`**（尽力而为），然后才尝试连接。这弥补了删除 `node-once.timer` 时一同失去的开机配置同步保证：即使中心节点不可达，`/etc/bird` 依然能够基于本地缓存进行渲染。
2. 连接 → 握手鉴权 → 发 `hello{cached_revision}` → 中心按需推送 desired。
3. 稳态下三个并发任务：收消息、每 60 秒发心跳、每 900 秒发一次全量对账请求。
4. 收到 `desired_push` 后：写缓存 → `apply()` → 上报 `apply_result`（payload 形状与 `node once` 完全一致）。apply 失败上报 `error` 并继续，不断连接。
5. 断线后按 **full jitter** 退避重连；**每轮重连都重读 `node.toml`**，所以 token 轮换后更新文件即可，无需 `systemctl restart`。

`node.toml` 的 `[agent]` 段（全部可选）：

```toml
[agent]
reconnect_initial_seconds  = 1.0      # 退避基数
reconnect_max_seconds      = 60.0     # 退避上限
auth_retry_seconds         = 300.0    # 鉴权类致命关闭后的固定重试间隔
reconcile_interval_seconds = 900.0    # 全量对账间隔
heartbeat_interval_seconds = 60.0     # 心跳间隔
```

鉴权类致命关闭（token 被轮换、节点被删除、协议版本不匹配等）使用固定的 `auth_retry_seconds`，避免过期 token 演变成对中心的高频重连。退避策略的完整设计见 `docs/architecture/sync_ws_protocol.md`。

退出码：`node.toml` 缺失或非法 → `2`；重试循环救不回来的运行时故障 → `1`；`SIGTERM` / `Ctrl-C` → `0`（让 `systemctl stop` 干净收尾）。

**急停**：`systemctl stop dn42ctl-node-agent`，之后仍可手动 `dn42ctl node once` / `pull` / `apply`。

### `dn42ctl node init --server <url> --node-id <id> --token <token>`

写入本机 `/etc/dn42ctl/node.toml`（`0600`）：

```toml
server  = "https://center.example"
node_id = "<id>"
token   = "<token>"

# [cache] 段可省略；省略时使用 /var/lib/dn42ctl/node-cache.sqlite3
# [apply] 段只有 reload = "auto" 或 "never"（默认 auto）一个键
```

- self 节点**不需要**手工 `init`，`dn42ctl serve` 启动时已经自动写好（`server = "http://[::1]:4242"`）。
- 不需要 root 时可加 `--config-path` 指向可写位置（继承现有 CLI 全局约定）。

### `dn42ctl node pull`

从 server 拉 desired state，写到本地缓存 `/var/lib/dn42ctl/node-cache.sqlite3`。**不写**任何系统配置文件。

### `dn42ctl node apply [--dry-run] [--from-server] [--no-reload]`

用本地缓存的 desired state 调现有 renderer 写入 `/etc/bird/...` / `/etc/systemd/network/...` 等。

- `--dry-run`：打印 diff（现有文件 vs 即将生成的内容），不写盘，也不 reload。
- `--from-server`：强制先 pull 再 apply（默认用最近一次缓存）。
- `--no-reload`：写盘后不执行 `networkctl reload` / `birdc configure`。
- 写盘使用 tmp+rename，失败不留半成品。

**desired state 带非空 `node` 块时**，apply 还会重写 `config.toml`、重渲 `bird.conf`、重写 `dn42-dummy.*`；没有该块时行为与本特性引入前完全一致。本地 `config.toml` 缺失或损坏则跳过这三步并告警；`dummy_backend = "nm"` 时跳过 `dn42-dummy.*` 并告警（该接口由 NetworkManager 管理，需在该节点上执行 `dn42ctl genconf`）。

**reload**：按实际写入/删除的路径决定——碰了 `networkd_dir` 就 `networkctl reload`，碰了 bird 相关文件就 `birdc configure`，什么都没变则一条都不执行。失败只记 warning，不中断 apply。也可用 `node.toml` 的 `[apply] reload = "never"` 永久关闭。完整规则见 [`../architecture/node_addressing.md`](../architecture/node_addressing.md) §8。

### `dn42ctl node push`

把一组结构化 proposals 推送到 server (`POST /api/v1/nodes/{id}/proposals`)。

- 输入 JSON 通过 `--json <file>`，文件顶层是数组：
  ```json
  [
    {"kind": "peer_add",    "payload": {"peer_kind": "bgp",  "peer": {...}}},
    {"kind": "peer_modify", "payload": {"peer_kind": "ibgp", "peer": {...}}},
    {"kind": "peer_delete", "payload": {"peer_kind": "bgp",  "key": {"peer_asn": ...}}}
  ]
  ```
- `--source push|scan` 标注来源（默认 `push`）。
- proposal 的 `kind` 与 payload schema 详见 `docs/architecture/sync_hub_spoke.md`。
- 计划中"自动扫描本机配置 → 与中心比对 → 自动判定 add/modify/delete"由 `dn42ctl node scan` 承担，目前尚未实现（见下）。

### `dn42ctl node scan`

> **尚未实现**。占位文档保留，待实现时本节会更新。
>
> 计划：复用现有 `dn42ctl scan` 的逻辑扫描本机 `/etc/systemd/network` 或 NetworkManager 连接，把扫到的 peer 信息转换为 proposals 推送给 server。与 `node push` 的区别：`push` 读 JSON 文件；`scan` 比对的是本机网络后端文件系统状态。
>
> 当前替代方案：手工生成 JSON 后用 `dn42ctl node push --source scan --json <file>` 推送。

### `dn42ctl node report`

单次上报本机状态（apply_result / live_status 等）到 `POST /api/v1/nodes/{id}/reports`。

### `dn42ctl node once`

= `pull && apply && report (apply_result)`。**一次性故障排查命令**。

- 状态同步由常驻 `dn42ctl node agent` 负责；本命令不再由任何 systemd timer 驱动（`dn42ctl-node-once.timer` 已删除）。
- 适用场景：agent 被 `systemctl stop` 之后的手动配置同步、验证 HTTP 通道是否可达、排查 agent 与手动 apply 结果是否一致。
- 任一步失败：整个命令以非零状态退出；失败时尝试上报 `kind=error` 的状态报告（尽力而为）。
- 不做指数退避（一次性命令，重试交给调用者）。
- `--no-report` 关闭自动 apply_result 上报。
- 与 agent 同时运行不会冲突：两者共用同一份缓存与同一套幂等的 `apply()`。

### `dn42ctl node status`

本地诊断 + 中心视角探活：

- 本地：node.toml 路径与权限、当前缓存 revision 与 fetched_at
- 远程：发起 `GET /api/v1/nodes/{id}/status`（5s 超时），打印中心视角的 `last_seen_at` / `current_revision` / `pinned_revision`
- 自动对比本地缓存 revision 与中心 `current_revision`，标记"同步"或"不一致"

---

## 与 `dn42ctl serve` 的关系

`dn42ctl serve` 不在本组命令下，但它的启动序列与 self 节点强相关：执行迁移、读取或创建 `/var/lib/dn42ctl/self_node_id`、UPSERT `managed_nodes` 中 `is_self=1` 的行、在 `node.toml` 与库中 hash 不一致时生成 self token，最后监听 `[::1]:4242` 并起 `sync_events` watcher。

`--no-self-register` 关闭其中的自动注册步骤。`--sync-poll-interval`（默认 1.0 秒）调整 watcher 轮询间隔，决定配置变更推送到节点的最大延迟。完整语义见 `docs/architecture/sync_hub_spoke.md`。
