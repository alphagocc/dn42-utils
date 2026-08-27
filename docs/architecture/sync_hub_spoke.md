# 多节点中心化同步（Hub-Spoke）

## 角色

| 角色 | 说明 |
|------|------|
| 中心主机（hub） | 运行 `dn42ctl serve`，持有权威 SQLite，是**唯一** source of truth |
| 远程节点（spoke） | 常驻 `dn42ctl node agent`，通过 WebSocket 长连接接收中心推送并应用配置 |
| self 节点 | 中心主机本身**也作为被管节点**之一，与远程节点同走一套协议，仅 server URL 不同（`http://[::1]:4242`）|

## 总体拓扑

```text
              管理员 CLI / Web UI
                      |
                      | HTTPS
                      v
              +-----------------+
              | nginx (反代)    |   <-- TLS / ACL / 限流 / WS Upgrade
              +--------+--------+
                       |  http, [::1]:4242
                       v
              +-----------------+
              | dn42ctl serve   |   <-- systemd 后台常驻 + sandbox
              | SQLite 权威 DB  |
              | sync_events     |   <-- watcher 每 1s 轮询,变更即推送
              +--------+--------+
                       ^
        +--------------+--------------+
        |              |              |
        |  WebSocket 长连接(双向)      | ws://[::1]:4242 (loopback,绕过 nginx)
        v              v              v
      节点 A         节点 B         self 节点 (中心主机自身)
   node agent      node agent     node agent
   ← desired_push  ← desired_push
   → report/proposal
   经 nginx WSS    经 nginx WSS
```

dn42ctl 自身**不处理 TLS 证书**。`dn42ctl serve` 仅监听 `[::1]:4242`；对外暴露与 TLS 终止由 nginx 承担。详见 `docs/architecture/deployment.md`。

## 传输通道

常驻 agent 使用 WebSocket，一次性 CLI 命令使用 HTTP。两条通道共用**同一套 Bearer token 鉴权**与**同一套 service 层**，语义完全等价；一次性 CLI 命令是独立进程，无法复用常驻 agent 持有的那条连接，因此 HTTP 路由保留。

通道划分表与协议细节（信封、消息目录、关闭码、生命周期、退避策略、`sync_events` 变更检测）见 `docs/architecture/sync_ws_protocol.md`。

## 数据所有权

- 中心 SQLite 是**唯一权威**。所有 `bgp_peers` / `ibgp_peers` 写入必须经过中心 service 层校验。
- 节点不能直接修改权威表。节点的 push / scan 进入 `config_proposals` 队列，等待管理员审核（或在 `write_policy.peer_add=auto_accept` 下立即走中心 service 校验并写入）。
- 节点的 apply / live status / error 进入 `node_reports`，仅事实陈述，**永远不直接修改业务表**。导入 `scan_result` 转为 peer 行是显式动作（`dn42ctl node import-report`）。

## 私钥策略（模式 A：中心托管）

- WireGuard 私钥保存在中心 SQLite，pull 时随 desired state 下发给节点。
- 选择模式 A 的理由：与现有 schema 一致；中心可独立备份恢复节点配置；首版实现最简。
- 安全前提：
  - 中心 SQLite 文件 `0600`，备份加密。
  - 节点 token 泄露**仅**暴露该 node_id 的私钥（详见鉴权章节）。
  - HTTPS 必须由 nginx 启用（self 节点走 loopback 不在此约束内）。
- 模式 B（节点本地私钥）作为未来高安全部署选项，**不在首版范围**。

## 鉴权模型

统一 Bearer token。admin 主体来自 `DN42CTL_API_TOKEN` 环境变量；node 主体由 `dn42ctl node token rotate <id>` 签发，SHA-256 hash 存入 `managed_nodes.api_token_hash`，作用域严格限制在 `/api/v1/nodes/{node_id}/...`，且 path 中的 `node_id` 必须等于 token 绑定的 node_id。完整主体表（含 auto-peer 的 peer-session）与 401 / 403 的语义划分见 `docs/architecture/rest_api.md`。

WS 通道在**握手时**验一次 token，之后每一帧读缓存的 principal，不再触碰 DB。

连接期间的 token 轮换、节点删除与禁用由 `sync_events` 的 `access_revoked` 事件驱动，server 随即用关闭码 `4003` 或 `4004` 断开该节点的连接。完整处理流程与 agent 的恢复行为见 `docs/architecture/sync_ws_protocol.md`。

## self 节点自动注册

`dn42ctl serve` 启动序列（幂等，可重复执行）：

1. 跑迁移（至 v13）。
2. 读 `/var/lib/dn42ctl/self_node_id`，不存在则生成 UUIDv4 写入（`0600`，owner=dn42ctl）。
3. `managed_nodes` UPSERT：先把其它行的 `is_self` 清零，再写入 `(node_id=<self>, name='self', is_self=1, enabled=1, write_policy=<默认 JSON>)`。清零这一步不可省：`self_node_id` 文件丢失后会生成新 UUID，只写不清就会留下两行 `is_self=1`，而 `get_self()` 无从判断该返回哪一行。旧行降级为普通受管节点，它的 peer 一条不动，可用 `dn42ctl node adopt-self` 搬迁。
4. 检查 `/etc/dn42ctl/node.toml` 与 `managed_nodes.api_token_hash` 是否一致：
   - 文件不存在 / `node_id` 不匹配 / `token` 缺失 / **token 与库中 hash 不符（含 hash 为 NULL）** → 生成 `secrets.token_urlsafe(32)`，hash 入库，明文写 `node.toml`（`0600`，owner=root，因为 node-agent.service 需要读）：
     ```toml
     server  = "http://[::1]:4242"
     node_id = "<self_node_id>"
     token   = "<明文 token>"
     ```
   - 两侧一致 → 不改动。
5. uvicorn 监听 `[::1]:4242`。

`--no-self-register` 关闭步骤 2-4（测试 / 不希望中心机自管的部署）。

self token 轮换：`dn42ctl node token rotate <self-id>` 同时更新 hash 与 self 的 `node.toml`。

### 步骤 4 必须真的比对 hash，不能只看文件是否存在

`node.toml` 的明文与 `api_token_hash` 是同一个凭据的两半，分别落在 `/etc` 与 `/var/lib`。任何只动其中一半的操作都会让它们分叉：

- SQLite 文件丢失后重建（`/etc` 与 `/var/lib` 常来自不同的备份快照）。
- 从**早于上一次 token 轮换**的备份恢复 DB。
- `dn42ctl node token rotate <self-id>` 已写库、但 `node.toml` 改写失败。
- `node remove --force` 删掉了行、`node.toml` 却没能删掉。

分叉之后 hub 自身的 agent 永久 401，并按 `auth_retry_seconds`（默认 300s）无限退避重试——hub 主机的配置就此停止更新，且 `api_token_hash` 非 NULL 的那两种场景没有任何外部信号。

把幂等性实现成"文件在就不管"，等于让 `serve` 永远发现不了这种分叉，重启也修不好。代价只是每次 `serve` 启动多做一次哈希校验，因此这里选择真的比对。

## 节点本地状态

- `/etc/dn42ctl/node.toml`（`0600`）：server URL / node_id / token / apply 路径覆盖 / `[agent]` 调优参数。
- `/var/lib/dn42ctl/node-cache.sqlite3`：缓存最近 desired state 与 revision。**仅缓存**，丢失不影响权威状态。agent 启动时会先用这份缓存跑一次 `apply()`，然后才尝试连接，以此保证 hub 不可达时 `/etc/bird` 仍会被渲染。该设计的完整论证见 `docs/architecture/sync_ws_protocol.md`。

## desired state JSON Schema

`GET /api/v1/nodes/{node_id}/desired` 返回：

```json
{
  "node_id": "uuid",
  "revision": "2026-05-18T10:00:00Z-001",
  "generated_at": "2026-05-18T10:00:00Z",
  "bgp_peers": [
    {
      "peer_asn": 4242420000,
      "ifname": "wg-peer-xxx",
      "wg_private_key": "...",
      "wg_public_key": "...",
      "peer_public_key": "...",
      "endpoint": "...",
      "local_lla": "fe80::...",
      "peer_lla": "fe80::...",
      "listen_port": 51820,
      "allowed_ips": ["fe80::/64", "fd00::/8"],
      "net_backend": "networkd"
    }
  ],
  "ibgp_peers": [
    {
      "name": "...",
      "ifname": "wg-ibgp-xxx",
      "wg_private_key": "...",
      "wg_public_key": "...",
      "peer_public_key": "...",
      "endpoint": "...",
      "local_lla": "...",
      "peer_lla": "...",
      "peer_ip": "fd00::...",
      "has_wg": true,
      "listen_port": 51820,
      "allowed_ips": ["::/0"],
      "net_backend": "networkd",
      "babel_rxcost": 120,
      "babel_type": "tunnel"
    }
  ],
  "paths": {
    "bird_conf_path": "/etc/bird/bird.conf",
    "peers_dir": "/etc/bird/peers/",
    "babel_conf_path": "/etc/bird/babel.conf",
    "networkd_dir": "/etc/systemd/network/",
    "nm_dir": "/etc/NetworkManager/system-connections/"
  },
  "node": {
    "own_ipv6": "fd42:4242:1234::1",
    "router_id": "172.20.1.1"
  }
}
```

- `paths` 是中心返回的默认值；节点 `node.toml [apply]` 段可覆盖。
- 字段语义与现有 `bgp_peers` / `ibgp_peers` 表一一对应。
- `node` 是**节点自身地址块**，来自 `managed_nodes` 的 `own_ipv6` / `router_id`。列为 NULL 时对应的键整个省略；**整块为空时 `node` 键本身不出现，且不参与内容哈希**（否则升级瞬间全网每个节点都会收到一次无意义推送）。收到非空 `node` 块的节点才会重写 `config.toml` / `bird.conf` / `dn42-dummy.*`。完整语义见 [`node_addressing.md`](node_addressing.md)。
- `endpoint_host` **不在**下发范围内——节点不会拨自己。

## 提案 / 上报 / 审核流程

### push 路径（节点推送配置变更）

```
node ──proposal_submit (WS) / POST /proposals (HTTP)──> server
                                              │
                       ┌──────────────────────┤
                       │ write_policy.kind=?  │
                       ├──────────────────────┘
                       ▼
        review                       auto_accept
          │                                │
          ▼                                ▼
  插入 config_proposals          走 service 层校验
  (status=pending)               ├─ ok    → 写权威表 + proposal=accepted
                                 └─ fail  → proposal=rejected(reason)
```

管理员后续：

```
dn42ctl node proposals <id>
dn42ctl node accept-proposal <pid>   # 走相同 service 校验
dn42ctl node reject-proposal <pid> --reason "..."
```

### report 路径（节点上报状态）

```
node ──report_submit (WS) / POST /reports (HTTP)──> server
                                            │
                                            ▼
                            插入 node_reports (永不自动改业务表)
                                            │
                                            ▼
                            管理员可显式 import-report (仅对 scan_result 类型)
```

agent 每次 apply 完成后自动上报 `apply_result`（payload 形状与 `dn42ctl node once` 一致），apply 失败时上报 `error`。

`write_policy.report=auto` 仅意味着 report 写入 `node_reports` 不需要审核；它**不**触发自动 import。

### proposal kind 判定

节点 push 时与中心当前权威表对比，自动标记 `kind`：

- 中心没有 → `peer_add`
- 中心有，字段不同 → `peer_modify`
- 节点本地删除 → `peer_delete`

## 同步语义

- 中心是 source of truth；节点向中心收敛。
- **推送式收敛**：权威表变更时 DB 层在同一事务内写一条 `sync_events`，server 的 watcher（默认 1 秒轮询）把它转成对应节点连接上的 `desired_push`。收敛延迟 ≤1 秒。
- **采用 level-triggered 语义**：节点连上时读取的是**当前**内容，事件仅触发一次重新检查；hub 用内容指纹与该连接已推送的指纹比对，相同则跳过推送。因此"事件与新连接竞态"不会丢更新，连接与 watcher 之间也无需游标协调。完整正确性论证见 `docs/architecture/sync_ws_protocol.md`。
- **兜底**：agent 每 900 秒主动发一次 `desired_request{reason:"reconcile"}` 做全量对账，防止长时间断连或指纹逻辑出错导致的漂移。
- 没有事件日志、CRDT、冲突合并：所有"冲突"都退化为中心 service 校验 + SQLite 约束。
- **提案 payload 在进入 service 层之前逐字段过一遍 validators**（`services/peer_payload.py`）。payload 是节点写入的任意 JSON，而 service 层只校验 `listen_port` / `net_backend` / `allowed_ips`——只靠后者的话，ASN、WireGuard 公钥、IPv6 地址会原样落库并渲染进该节点的 `bird.conf`，而 `include "<peers_dir>/*";` 会让一条非法 peer 拖垮整份配置。详见 [`validation.md`](validation.md)。
- `peer_modify` / `peer_delete` **始终** review，不支持 auto_accept（避免节点被入侵后污染权威表）。
- 节点重启 / 重装：拿回 token 后 `dn42ctl node init` → agent 启动即恢复完整状态。

## 安全要求

- server 监听 `[::1]:4242`，CLI 检测到非 loopback host 时打 warning。
- admin token 与 node token 严格分隔；node token 越权返回 403，WS 通道对应关闭码 `4403`。
- node.toml / server.env / SQLite 全部 `0600`。
- desired state 含 WireGuard 私钥 → 远程节点必须经 nginx HTTPS/WSS；self 节点经 loopback。
- 节点 apply 只写本机配置文件，不修改路由表（沿用 `docs/spec.md` 既有约束）。
- report 不触发任何系统命令；proposal 不绕过中心 service 校验。
- **常驻 agent 是 7×24 的 root 进程**，节点 token 与全部 WG 私钥常驻其内存；相比原先每 10 分钟约 1 秒的 oneshot，这是纵深防御的实质性降低。量化对比与缓解措施见 `docs/architecture/deployment.md`。

## 交叉引用

- WebSocket 协议：`docs/architecture/sync_ws_protocol.md`
- 表结构：`docs/architecture/database.md`
- REST 路由与鉴权细节：`docs/architecture/rest_api.md`
- systemd unit / nginx 反代 / 部署流程：`docs/architecture/deployment.md`
- CLI 详细参数：`docs/commands/node.md`
