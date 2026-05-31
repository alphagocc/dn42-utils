# REST API

dn42ctl 提供可选的 REST API 模式，通过 `dn42ctl serve` 启动 HTTP 服务器。

## 运行方式

```bash
dn42ctl serve [--host ::1] [--port 4242]
        # admin token 必须通过环境变量 DN42CTL_API_TOKEN 提供
```

- **默认绑定 `[::1]`**（IPv6 loopback），端口 `4242`。
- **dn42ctl 不处理 TLS 证书。** 对外暴露与 HTTPS 终止由 nginx 反代承担——CLI 不接受 `--tls-cert` / `--tls-key`，且对非 loopback `--host` 会打 warning。
- 部署细节（systemd unit + nginx 反代示例）见 `docs/architecture/deployment.md`。

## 鉴权（admin / node / peer-session 三类 principal）

`/api/...` 与 `/api/admin/...` 与 `/api/v1/nodes/...` 走 `Authorization: Bearer <token>` 头；`/api/public/auto-peer/...` 中的前 3 步公开，仅 `submit` 需要一个短期 peer-session bearer。

| 主体 | token 来源 | 可访问 |
|------|-----------|--------|
| **admin** | 环境变量 `DN42CTL_API_TOKEN`（部署时随机生成） | `/api/...` 全部既有路由 + `/api/admin/...` |
| **node** | `dn42ctl node token rotate <id>` 签发；argon2id hash 存 `managed_nodes.api_token_hash`，明文只在签发时返回一次 | 仅 `/api/v1/nodes/{node_id}/...`，且 path 中的 `node_id` 必须等于 token 绑定的 node_id |
| **peer-session** | `/api/public/auto-peer/verify` 校验通过后签发，TTL 15 分钟、in-memory、绑定到 verified_asn | 仅 `/api/public/auto-peer/submit`，且 `peer_asn` 必须等于 token 绑定的 ASN |

错误码：

- `401 Unauthorized` — 缺 token / token 不可解析。
- `403 Forbidden` — token 有效但越权（node token 试图访问其他 node_id，或访问 admin 路由）。

token hash 比对走恒定时间。self 节点的 token 由 `dn42ctl serve` 启动时自动签发并明文写入 `/etc/dn42ctl/node.toml`；与远程节点 token 走同一套校验路径。

## 路由表

### 公共路由（无认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/version` | 返回版本号与 git commit hash |

### 现有路由（admin token）

所有路由均在 `/api/` 下：

| 方法 | 路径 | 对应 CLI | 说明 |
|------|------|---------|------|
| `GET` | `/api/bgp/peers` | `show bgp` | 列出所有 BGP peer（支持 `?live=false` 跳过探测） |
| `POST` | `/api/bgp/peers` | `bgp peer` | 创建 BGP peer |
| `PUT` | `/api/bgp/peers/{asn}` | `bgp peer modify` | 修改 BGP peer |
| `DELETE` | `/api/bgp/peers/{asn}` | `bgp peer del` | 删除 BGP peer |
| `GET` | `/api/ibgp/peers` | `show ibgp` | 列出所有 iBGP peer（支持 `?live=false`） |
| `POST` | `/api/ibgp/peers` | `ibgp peer` | 创建 iBGP peer |
| `PUT` | `/api/ibgp/peers/{name}` | `ibgp peer modify` | 修改 iBGP peer |
| `DELETE` | `/api/ibgp/peers/{name}` | `ibgp peer del` | 删除 iBGP peer |
| `GET` | `/api/wg/tunnels` | `show wg` | 列出所有 WireGuard 隧道（支持 `?live=false`） |
| `GET` | `/api/show/all` | `show all` | 聚合视图：node_id + wg/bgp/ibgp 列表 |
| `POST` | `/api/genconf` | `genconf` | 重新生成 Bird/Babel/ROA 配置 |

### 节点路由（node token，`node_id` 必须匹配）

| 方法 | 路径 | 对应 CLI | 说明 |
|------|------|---------|------|
| `GET` | `/api/v1/nodes/{node_id}/desired` | `node pull` | 返回该节点的 desired state JSON |
| `POST` | `/api/v1/nodes/{node_id}/proposals` | `node push` / `node scan` | 推送配置提案，依 `write_policy` 进队或自动走校验 |
| `POST` | `/api/v1/nodes/{node_id}/reports` | `node report` | 上报 apply 结果 / scan 结果 / live status / error |
| `GET` | `/api/v1/nodes/{node_id}/status` | `node status` | 中心视角看到的该节点最近 revision / last_seen 等 |

desired state JSON schema 详见 `docs/architecture/sync_hub_spoke.md`。

### 管理员路由（admin token）

| 方法 | 路径 | 对应 CLI | 说明 |
|------|------|---------|------|
| `GET` | `/api/admin/nodes` | `node list` | 列出所有 managed_nodes |
| `POST` | `/api/admin/nodes` | `node add` | 注册新节点 |
| `GET` | `/api/admin/nodes/{node_id}` | `node show` | 查看单节点详情 |
| `DELETE` | `/api/admin/nodes/{node_id}` | `node remove` | 注销节点（self 节点需 `?force=true`） |
| `POST` | `/api/admin/nodes/{node_id}/token` | `node token rotate` | 重签 node token，返回明文一次 |
| `PATCH` | `/api/admin/nodes/{node_id}/policy` | `node policy set` | 修改 `write_policy` JSON |
| `GET` | `/api/admin/nodes/{node_id}/proposals` | `node proposals` | 列出该节点的提案 |
| `POST` | `/api/admin/proposals/{proposal_id}/accept` | `node accept-proposal` | 接受提案，走 service 校验 |
| `POST` | `/api/admin/proposals/{proposal_id}/reject` | `node reject-proposal` | 拒绝提案 |
| `GET` | `/api/admin/nodes/{node_id}/reports` | `node reports` | 列出该节点的上报 |
| `POST` | `/api/admin/reports/{report_id}/import` | `node import-report` | 从 scan_result 导入 peer |
| `GET` | `/api/admin/nodes/{node_id}/revisions` | `node revisions` | 列出 desired state 历史快照（阶段 5） |
| `POST` | `/api/admin/nodes/{node_id}/rollback` | `node rollback` | 切换当前 desired state 到指定 revision（阶段 5） |

### 公共路由（无 bearer / peer-session bearer）

启用条件：`config.toml` 中设置了 `dn42_registry_path`。未配置时所有 `/api/public/auto-peer/*` 返回 503。详见 `docs/architecture/auto_peer.md`。

| 方法 | 路径 | Bearer | 说明 |
|------|------|--------|------|
| `POST` | `/api/public/auto-peer/lookup` | （无） | 输入 ASN，返回该 AS 的 mnt-by 列表及每个 mntner 中受支持的 `auth:` 选项 |
| `POST` | `/api/public/auto-peer/challenge` | （无） | 选定 mntner + auth_index，返回 `challenge_id` 与 32 字节随机 nonce（hex），TTL 10 分钟、一次性 |
| `POST` | `/api/public/auto-peer/verify` | （无） | 提交签名；服务端通过 `ssh-keygen -Y verify` 或 `gpg --verify` 校验，成功后返回 `peer_session_token`（TTL 15 分钟，绑定到该 ASN） |
| `POST` | `/api/public/auto-peer/submit` | peer-session | 提交 WG pubkey/endpoint/peer_lla 等字段，服务端转换为 `peer_add` proposal 写入 self 节点队列 |

peer-session bearer 与 admin / node token 走完全独立的解析路径，作用域仅限 `/api/public/auto-peer/submit`，使用 in-memory TTL store 管理。
- `init` 涉及交互式配置创建和系统接口初始化。
- `scan` 涉及扫描本地文件系统并修改 config.toml。

> 注：`dn42ctl node scan` / `dn42ctl node push` 在节点侧执行，通过 `POST /api/v1/nodes/{id}/proposals` 把扫描结果**作为提案**推送给中心；这与中心 admin 的 `scan` 命令是两回事。

## 错误处理

- 服务层 `Dn42CtlError` → HTTP `400` + `{"detail": "..."}`
- 鉴权失败 → HTTP `401`
- 越权（token 有效但 path node_id 不匹配 / 访问 admin 路由） → HTTP `403`
- 输入格式/类型校验失败（Pydantic `field_validator`） → HTTP `422` + `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`
- 路径参数无效 → HTTP `422`（FastAPI 自动校验）

详见 `docs/architecture/validation.md`。

## 技术栈

- 框架：FastAPI
- ASGI 服务器：Uvicorn
- 请求/响应模型：Pydantic v2
- token hash：argon2id（`argon2-cffi`）
