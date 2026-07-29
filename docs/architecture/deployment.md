# 部署：systemd + nginx

dn42ctl 在生产环境以两类 systemd unit 运行：

- `dn42ctl-server.service` —— 中心主机 hub，常驻 API server。
- `dn42ctl-node-agent.service` —— 任何节点（含 self）的常驻同步 agent，持有到 hub 的 WebSocket 长连接。

unit 模板与 nginx 反代示例位于项目根 `systemd/` 目录。

架构背景见 `docs/architecture/sync_hub_spoke.md`，同步协议见 `docs/architecture/sync_ws_protocol.md`。

## 文件清单

```
systemd/
├── dn42ctl-server.service          # 中心主机: dn42ctl serve 常驻
├── dn42ctl-node-agent.service      # 任何节点: dn42ctl node agent 常驻
├── nginx.dn42ctl.conf.example      # nginx 三子域名反代示例 (含 WS location)
└── server.env.example              # server.env 模板
```

## 设计原则

- **server 不碰系统配置**：`dn42ctl serve` 只读写权威 SQLite 与 self 的 `node.toml`。`/etc/bird` / `/etc/systemd/network` 等渲染目标由 `dn42ctl-node-agent.service` 处理。两者职责彻底分离，让 server unit 能用最严的 sandbox。
- **server 只监听 loopback**：TLS / 对外暴露完全交给 nginx。dn42ctl 不接受 `--tls-cert` / `--tls-key`。
- **self 节点不走 nginx**：`node.toml` 中 `server = "http://[::1]:4242"`，直连 uvicorn。
- **node-agent 自带重连退避**：不依赖 systemd 重试，因此 `StartLimitIntervalSec=0` 关掉 systemd 的熔断，
  让 `Restart=always` 永远生效。

## 安全姿态变化（从 timer 迁移到常驻 agent）

原先的 `dn42ctl-node-once.timer` 每 10 分钟拉起一个约 1 秒的 root oneshot；现在是一个 7×24 的
root 常驻进程。这是一次**有意识的权衡**，必须明确认可：

| | 之前（`node-once.timer`） | 之后（`node-agent.service`） |
|---|---|---|
| root 进程存活时间 | 每 10 分钟约 1 秒 | **7×24 常驻** |
| hub→spoke 写入延迟 | ≤10 分钟，**spoke 发起** | ≤1 秒，**hub 发起** |
| root 内存中常驻的秘密 | 瞬时 | node token + **全部 WG 私钥**，永久 |
| hub 被攻陷的爆炸半径 | 延迟、有界的配置写入 | 每个 spoke 上近乎即时的 root 级配置写入，随时可用 |
| 失败模式 | timer 下一轮重试 | `Restart=always` 崩溃循环触碰 `/etc`（由 `RestartSec=5s` 限速） |

净效果是纵深防御的实质性降低，换取亚秒级收敛。配套缓解：

- agent unit 的 sandbox 比原 `node-once.service` **更严**（见下）。
- hub 仍是唯一权威，节点无法直接改权威表。
- **逃生通道**：`systemctl stop dn42ctl-node-agent` 之后仍可手动
  `dn42ctl node once` / `pull` / `apply`（HTTP 路由保留）。

## systemd unit 说明

详细内容见 `systemd/` 目录下的文件，这里只记录关键设计决策。

### dn42ctl-server.service

- 以专用用户 `dn42ctl` 运行（非 root）。
- `EnvironmentFile=/etc/dn42ctl/server.env` 注入 `DN42CTL_API_TOKEN` 和 `DN42CTL_CORS_ORIGINS`。
- 严格 sandbox：`ProtectSystem=strict`、清空 `CapabilityBoundingSet`、`IPAddressAllow=localhost` + `IPAddressDeny=any`（强制仅 loopback 通信）。
- 调试时先 `journalctl -u dn42ctl-server` 查报错，再有针对性地放宽 sandbox 指令。

### dn42ctl-node-agent.service

- 必须以 root 运行（需写 `/etc/bird` 等，调用 `wg` / `ip` / `nmcli`），sandbox 比 server 宽松。
- `Type=exec` + `Restart=always` + `RestartSec=5s`；`[Unit] StartLimitIntervalSec=0`
  关掉 systemd 熔断——agent 自己有指数退避，不该被 systemd 判定为"反复失败"而停掉。
- sandbox 在原 `node-once.service` 基础上收紧：`UMask=0077`、`MemoryDenyWriteExecute=true`、
  `ProtectProc=invisible`、`RestrictNamespaces=true`、`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`。
- **刻意不清空 `CapabilityBoundingSet`**：不同于 server，这个进程要对
  `/etc/systemd/network/*.netdev` 调 `chown`。若要收窄到 `CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER`，
  请先在目标环境实测验证。
- **中心主机上**额外投一个 drop-in（不能放进共享 unit，因为 spoke 上没有 server）：

  ```ini
  # /etc/systemd/system/dn42ctl-node-agent.service.d/hub.conf
  [Unit]
  After=dn42ctl-server.service
  Wants=dn42ctl-server.service
  ```

  没有它 agent 也能工作（退避重连会兜住），只是开机日志会干净一些。

### 开机收敛

删掉 timer 的同时也删掉了它的 `OnBootSec=2min`。作为补偿，**agent 在尝试第一次连接之前，
会先用本地缓存（`/var/lib/dn42ctl/node-cache.sqlite3`）跑一次 `apply()`**。
所以即使 spoke 重启时 hub 不可达，`/etc/bird` 仍会被渲染。这不是可选优化，
是"无 timer 兜底"这个设计能成立的前提。

## nginx 反代示例

采用三子域名部署（`api.` / `admin.` / `peer.`）。详见 `systemd/nginx.dn42ctl.conf.example`。

核心要点：

- **API 子域名**：反代到 `[::1]:4242`（uvicorn），无静态文件。
- **WebSocket**：节点 agent 的 `/api/v1/nodes/{id}/ws` 需要单独一条**正则** location，
  透传 `Upgrade` / `Connection` 头并把 `proxy_read_timeout` 放宽到 3600s：

  ```nginx
  location ~ ^/api/v1/nodes/[^/]+/ws$ {
      proxy_pass         http://[::1]:4242;
      proxy_http_version 1.1;
      proxy_set_header   Upgrade    $http_upgrade;
      proxy_set_header   Connection "upgrade";
      proxy_set_header   Host              $host;
      proxy_set_header   X-Forwarded-For   $remote_addr;
      proxy_set_header   X-Forwarded-Proto $scheme;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
  }
  ```

  **必须用正则而不是在 `location /` 上统一强制 upgrade**：兄弟路由
  `/api/v1/nodes/{id}/desired|proposals|reports|status` 共享同一前缀，强制 upgrade 会把它们打断。
  nginx 中正则 location 优先于前缀 location，所以 `location /` 原样不动。
- **admin / peer 子域名**：各自 `try_files $uri /{admin,peer}/index.html`，`root /var/www/dn42ctl`。
- **CORS**：前端跨域访问 API 子域名，需要在 `server.env` 中设置 `DN42CTL_CORS_ORIGINS`。
- **构建时**：需设置 `VITE_API_BASE` 环境变量指向 API 子域名。

## 首次部署流程

### 中心主机

```bash
# 1. 系统用户与目录
sudo useradd -r -s /usr/sbin/nologin dn42ctl
sudo install -d -m 0750 -o dn42ctl -g dn42ctl /var/lib/dn42ctl /etc/dn42ctl

# 2. 安装 dn42ctl 到 /usr/local/bin
sudo dn42ctl deploy daemon

# 3. server.env (admin token + CORS origins)
sudo install -m 0600 -o dn42ctl -g dn42ctl /dev/stdin /etc/dn42ctl/server.env <<EOF
DN42CTL_API_TOKEN=$(openssl rand -hex 32)
DN42CTL_CORS_ORIGINS=https://admin.dn42.example.com,https://peer.dn42.example.com
EOF

# 4. dn42ctl init (初始化配置与数据库)
sudo -u dn42ctl dn42ctl init

# 5. systemd units
sudo cp systemd/dn42ctl-server.service /etc/systemd/system/
sudo cp systemd/dn42ctl-node-agent.service /etc/systemd/system/
sudo install -d /etc/systemd/system/dn42ctl-node-agent.service.d
printf '[Unit]\nAfter=dn42ctl-server.service\nWants=dn42ctl-server.service\n' \
  | sudo tee /etc/systemd/system/dn42ctl-node-agent.service.d/hub.conf
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-server.service
sudo systemctl enable --now dn42ctl-node-agent.service

# 6. Web UI 构建与部署
sudo dn42ctl deploy web --api-base https://api.dn42.example.com /var/www/dn42ctl

# 7. nginx
sudo cp systemd/nginx.dn42ctl.conf.example /etc/nginx/conf.d/dn42ctl.conf
# 编辑 server_name / 证书路径
sudo nginx -t && sudo systemctl reload nginx
```

### 远程被管节点

```bash
# 管理员在中心主机:
dn42ctl node add <new-node-id> --name <hostname>
dn42ctl node token rotate <new-node-id>     # 记下明文 token

# 节点主机:
sudo dn42ctl deploy daemon
dn42ctl node init --server https://api.dn42.example.com --node-id <id> --token <token>

sudo cp systemd/dn42ctl-node-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-node-agent.service

# 验证: 应在 1 秒内看到握手 + 初始同步
journalctl -fu dn42ctl-node-agent
```

> 中心主机上跑 CLI 写命令时建议用 `sudo -u dn42ctl dn42ctl ...`，与 server 进程保持同一个
> 文件 owner，避免 SQLite 文件权限漂移。

## 从 node-once.timer 升级

hub 与所有 spoke 需要**一起升级**（沿用"所有节点运行统一版本"的既有约定）。

```bash
# 每个节点（含 self）:
sudo systemctl disable --now dn42ctl-node-once.timer
sudo rm -f /etc/systemd/system/dn42ctl-node-once.{service,timer}

sudo dn42ctl deploy daemon                       # 升级二进制
sudo cp systemd/dn42ctl-node-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-node-agent.service

# 中心主机额外:
sudo systemctl restart dn42ctl-server            # 跑 migration v9 + 起 watcher
# nginx 加上 WS location 后
sudo nginx -t && sudo systemctl reload nginx
```

`dn42ctl node once` / `pull` / `push` / `report` / `status` 这些一次性命令**保留可用**，
用于人工排障。

## self node 自动注册流程

```
systemctl start dn42ctl-server.service
            │
            ▼
   dn42ctl serve 启动
            │
            ▼
   ┌─────────────────────┐
   │ 1. 跑迁移 (至 v9)   │
   └──────────┬──────────┘
              ▼
   ┌─────────────────────────────────────┐
   │ 2. /var/lib/dn42ctl/self_node_id    │
   │    不存在?  → 生成 UUIDv4 + 写文件  │
   └──────────┬──────────────────────────┘
              ▼
   ┌─────────────────────────────────────┐
   │ 3. UPSERT managed_nodes             │
   │    (is_self=1, name='self', ...)    │
   └──────────┬──────────────────────────┘
              ▼
   ┌─────────────────────────────────────┐
   │ 4. /etc/dn42ctl/node.toml           │
   │    缺失 / 不匹配 / 缺 token?        │
   │    → 生成 token,hash 入库,         │
   │      明文写 node.toml (0600)        │
   └──────────┬──────────────────────────┘
              ▼
   ┌─────────────────────────────────────┐
   │ 5. uvicorn 监听 [::1]:4242         │
   │    + sync_events watcher 后台任务   │
   └─────────────────────────────────────┘
```

第一次 `enable --now` 后 self 节点完全就绪；后续 restart 幂等（不会重新生成 token）。

`--no-self-register` 关闭步骤 2-4，适用于测试或不希望中心机自管的部署。

## token 轮换

```bash
# admin token: 改 /etc/dn42ctl/server.env -> systemctl restart dn42ctl-server
# 注意:节点 token 不受影响,但所有正在用旧 admin token 的请求立即失效

# 节点 token (任意节点):
dn42ctl node token rotate <node-id>     # 打印新 token 明文
# 中心会立即用关闭码 4003 断开该节点的 WS 连接
# 若是 self 节点: /etc/dn42ctl/node.toml 自动同步更新,agent 下一轮重连即恢复
# 若是远程节点: 把新 token 安全送达对端,在对端
#   dn42ctl node init --server ... --node-id ... --token <new>
# 重新覆写 /etc/dn42ctl/node.toml
# agent 每轮重连都重读 node.toml,所以 **无需** systemctl restart
```

轮换后 agent 会进入 300 秒的长退避（防止过期 token 变成对 hub 的 argon2 DoS）。
想立刻恢复就 `sudo systemctl restart dn42ctl-node-agent`。
