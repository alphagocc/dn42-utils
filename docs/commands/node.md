# 命令：node

`dn42ctl node` 是中心化同步的命令组。**admin 子命令**（中心主机执行）与**节点子命令**（spoke 主机执行）混在同一个 group 下，靠第二级动词区分。Typer 不会冲突。

详细架构见 `docs/architecture/sync_hub_spoke.md`。

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

### `dn42ctl node token rotate <node-id>`

重签 node token：

1. 生成 `secrets.token_urlsafe(32)`。
2. argon2id hash 写 `managed_nodes.api_token_hash`。
3. **明文 token 仅在此命令返回时打印一次**。
4. 若 `is_self=1`：同步重写中心主机的 `/etc/dn42ctl/node.toml`。

旧 token 立即失效。

### `dn42ctl node policy set <node-id> [选项]`

修改 `write_policy` JSON。选项：

- `--peer-add review|auto_accept`
- `--peer-modify review`（仅接受 `review`，schema 不允许 auto）
- `--peer-delete review`（仅接受 `review`）
- `--report auto|review`

未指定的字段不变。

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

### `dn42ctl node revisions <node-id>`（阶段 5）

列出该节点的 desired state 历史快照，按 `generated_at` 倒序。

### `dn42ctl node rollback <node-id> --to <revision>`（阶段 5）

把该节点的"当前期望"指向指定 revision。下次 pull 返回该 revision 的 payload。

---

## 节点子命令（在 spoke 主机执行）

### `dn42ctl node init --server <url> --node-id <id> --token <token>`

写入本机 `/etc/dn42ctl/node.toml`（`0600`）：

```toml
server  = "https://center.example"
node_id = "<id>"
token   = "<token>"

# [apply] / [cache] 段使用 paths.py 的默认值,可手工补充覆盖
```

- self 节点**不需要**手工 `init` —— `dn42ctl serve` 启动时已经自动写好（`server = "http://[::1]:4242"`）。
- 不需要 root 时可加 `--config-path` 指向可写位置（继承现有 CLI 全局约定）。

### `dn42ctl node pull`

从 server 拉 desired state，写到本地缓存 `/var/lib/dn42ctl/node-cache.sqlite3`。**不写**任何系统配置文件。

### `dn42ctl node apply [--dry-run] [--from-server]`

用本地缓存的 desired state 调现有 renderer 写入 `/etc/bird/...` / `/etc/systemd/network/...` 等。

- `--dry-run`：打印 diff（现有文件 vs 即将生成的内容），不写盘。
- `--from-server`：强制先 pull 再 apply（默认用最近一次缓存）。
- 写盘使用 tmp+rename，失败不留半成品。

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

= `pull && apply && report (apply_result)`。供 `dn42ctl-node-once.timer` 调用。

- 任一步失败：整个命令以非零退出，由 timer 下一轮重试；失败时尽量上报 `kind=error` 的 report（best-effort）。
- 不做指数退避（timer `OnUnitActiveSec=10min` 已经够稳）。
- `--no-report` 关闭自动 apply_result 上报。

### `dn42ctl node status`

本地诊断 + 中心视角探活：

- 本地：node.toml 路径与权限、当前缓存 revision 与 fetched_at
- 远程：发起 `GET /api/v1/nodes/{id}/status`（5s 超时），打印中心视角的 `last_seen_at` / `current_revision` / `pinned_revision`
- 自动对比本地缓存 revision 与中心 `current_revision`，标记"同步"或"不一致"

---

## 与 `dn42ctl serve` 的关系

`dn42ctl serve` 不在本组命令下，但它的启动序列与 self 节点强相关：

1. 跑迁移。
2. 读 / 创 `/var/lib/dn42ctl/self_node_id`。
3. UPSERT `managed_nodes` 中 `is_self=1` 的行。
4. 若 `/etc/dn42ctl/node.toml` 缺失或不匹配，生成 self token 写入。
5. 监听 `[::1]:4242`。

`--no-self-register` 关闭步骤 2-4。详见 `docs/architecture/sync_hub_spoke.md`。
