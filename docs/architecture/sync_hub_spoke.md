# 多节点中心化同步（Hub-Spoke）

> 历史与替代方案讨论见仓库根目录 `plan.md`、`plan-reviewed.md`。本文是落地后的权威架构说明。

## 角色

| 角色 | 说明 |
|------|------|
| 中心主机（hub） | 运行 `dn42ctl serve`，持有权威 SQLite，是**唯一** source of truth |
| 远程节点（spoke） | 通过 HTTPS 访问中心 API，定时 `dn42ctl node once` 拉取并应用配置 |
| self 节点 | 中心主机本身**也作为被管节点**之一，与远程节点同走 HTTP 路径，仅 server URL 不同（`http://[::1]:4242`）|

## 总体拓扑

```text
              管理员 CLI / Web UI
                      |
                      | HTTPS
                      v
              +-----------------+
              | nginx (反代)    |   <-- TLS / ACL / 限流
              +--------+--------+
                       |  http, [::1]:4242
                       v
              +-----------------+
              | dn42ctl serve   |   <-- systemd 后台常驻 + sandbox
              | SQLite 权威 DB  |
              +--------+--------+
                       ^
        +--------------+--------------+
        |              |              |
        |              |              | http://[::1]:4242 (loopback,绕过 nginx)
        v              v              v
      节点 A         节点 B         self 节点 (中心主机自身)
   pull/apply      pull/apply     pull/apply
   push/report     push/report    push/report
   经 nginx HTTPS  经 nginx HTTPS
```

dn42ctl 自身**不处理 TLS 证书**。`dn42ctl serve` 仅监听 `[::1]:4242`；对外暴露与 TLS 终止由 nginx 承担。详见 `docs/architecture/deployment.md`。

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

统一 Bearer token，区分两种主体：

| 主体 | token 来源 | 可访问 |
|------|-----------|--------|
| admin | `DN42CTL_API_TOKEN` 环境变量 | `/api/admin/...` + 所有现有 `/api/...` 既有路由 |
| node | `dn42ctl node token rotate <id>` 签发，hash 入 `managed_nodes.api_token_hash` | 仅 `/api/v1/nodes/{node_id}/...` 且 `node_id` 必须等于 token 绑定的 node_id |

错误码：

- `401 Unauthorized` — 缺 token / token 不可解析。
- `403 Forbidden` — token 有效但越权访问其他 node_id 或试图调 admin 路由。

token hash 用 argon2id 存储，比对走恒定时间。

## self 节点自动注册

`dn42ctl serve` 启动序列（幂等，可重复执行）：

1. 跑迁移（含 v5）。
2. 读 `/var/lib/dn42ctl/self_node_id`，不存在则生成 UUIDv4 写入（`0600`，owner=dn42ctl）。
3. `managed_nodes` UPSERT：`(node_id=<self>, name='self', is_self=1, enabled=1, write_policy=<默认 JSON>)`。
4. 检查 `/etc/dn42ctl/node.toml`：
   - 文件不存在 / `node_id` 不匹配 / `token` 缺失 → 生成 `secrets.token_urlsafe(32)`，hash 入库，明文写 `node.toml`（`0600`，owner=root，因为 node-once.service 需要读）：
     ```toml
     server  = "http://[::1]:4242"
     node_id = "<self_node_id>"
     token   = "<明文 token>"
     ```
   - 已存在且匹配 → 不改动。
5. uvicorn 监听 `[::1]:4242`。

`--no-self-register` 关闭步骤 2-4（测试 / 不希望中心机自管的部署）。

self token 轮换：`dn42ctl node token rotate <self-id>` 同时更新 hash 与 self 的 `node.toml`。

## 节点本地状态

- `/etc/dn42ctl/node.toml`（`0600`）：server URL / node_id / token / apply 路径覆盖。
- `/var/lib/dn42ctl/node-cache.sqlite3`：缓存最近 desired state 与 revision。**仅缓存**，丢失不影响权威状态。

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
  }
}
```

- `paths` 是中心返回的默认值；节点 `node.toml [apply]` 段可覆盖。
- 字段语义与现有 `bgp_peers` / `ibgp_peers` 表一一对应。

## 提案 / 上报 / 审核流程

### push 路径（节点推送配置变更）

```
node ──POST /api/v1/nodes/{id}/proposals──> server
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
node ──POST /api/v1/nodes/{id}/reports──> server
                                            │
                                            ▼
                            插入 node_reports (永不自动改业务表)
                                            │
                                            ▼
                            管理员可显式 import-report (仅对 scan_result 类型)
```

`write_policy.report=auto` 仅意味着 report 写入 `node_reports` 不需要审核；它**不**触发自动 import。

### proposal kind 判定

节点 push 时与中心当前权威表对比，自动标记 `kind`：

- 中心没有 → `peer_add`
- 中心有，字段不同 → `peer_modify`
- 节点本地删除 → `peer_delete`

## 同步语义

- 中心是 source of truth；节点 pull 后向中心收敛。
- 没有事件日志、CRDT、冲突合并：所有"冲突"都退化为中心 service 校验 + SQLite 约束。
- `peer_modify` / `peer_delete` **始终** review，不支持 auto_accept（避免节点被入侵后污染权威表）。
- 节点重启 / 重装：拿回 token 后 `dn42ctl node init` → 下一次 once 即可恢复完整状态。

## 安全要求

- server 监听 `[::1]:4242`，CLI 检测到非 loopback host 时打 warning。
- admin token 与 node token 严格分隔；node token 越权返回 403 而非 401。
- node.toml / server.env / SQLite 全部 `0600`。
- desired state 含 WireGuard 私钥 → 远程节点必须经 nginx HTTPS；self 节点经 loopback。
- 节点 apply 只写本机配置文件，不修改路由表（沿用 `docs/spec.md` 既有约束）。
- report 不触发任何系统命令；proposal 不绕过中心 service 校验。

## 交叉引用

- 表结构：`docs/architecture/database.md`
- REST 路由与鉴权细节：`docs/architecture/rest_api.md`
- systemd unit / nginx 反代 / 部署流程：`docs/architecture/deployment.md`
- CLI 详细参数：`docs/commands/node.md`
