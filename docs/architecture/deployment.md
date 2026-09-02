# 部署：systemd + nginx

dn42ctl 在生产环境以两类 systemd unit 运行：

- `dn42ctl-server.service`：中心主机 hub，常驻 API server。
- `dn42ctl-node-agent.service`：任何节点（含 self）的常驻同步 agent，持有到 hub 的 WebSocket 长连接。

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
- **self 节点绕过 nginx**：`node.toml` 中 `server = "http://[::1]:4242"`，直连 uvicorn。
- **node-agent 自带重连退避**：不依赖 systemd 重试，因此 `StartLimitIntervalSec=0` 关掉 systemd 的熔断，让 `Restart=always` 永远生效。

## 常驻代理的安全代价

`node-agent.service` 是全天候运行的 root 进程。相比原先每 10 分钟约 1 秒的 oneshot，纵深防御有实质性降低。

API token 与该节点全部 WireGuard 私钥在服务运行期间始终驻留内存。配置推送延迟在 1 秒以内，hub 一旦被攻破，攻击者能近乎实时地在所有节点触发 root 级别的配置写入。`Restart=always` 保证崩溃后自动恢复，`RestartSec=5s` 防止死循环对 `/etc` 的高频覆写。

缓解手段：agent unit 配置了严格的 systemd sandbox（详见下方 unit 说明）；hub 是唯一权威数据源，节点无权修改权威表；需要隔离常驻进程时可以停止 agent，保留的 HTTP 路由仍然支持手动 `node pull` / `node apply`。

在启用 SELinux 的系统上还有第二层限制：`selinux/` 提供的策略模块把两个服务分别放进 `dn42ctl_server_t` 与 `dn42ctl_agent_t`，按文件类型而非目录约束 agent 的写入范围。详见 [`selinux.md`](selinux.md)。

## systemd unit 说明

详细内容见 `systemd/` 目录下的文件，这里只记录关键设计决策。

### dn42ctl-server.service

- 以专用用户 `dn42ctl` 运行（非 root）。
- `EnvironmentFile=/etc/dn42ctl/server.env` 注入 `DN42CTL_API_TOKEN`、`DN42CTL_CORS_ORIGINS` 与 `DN42CTL_AUTOPEER_DOMAIN`。后者是 auto-peer 向导页面的公开域名，同时充当公共接口的开关，详见 [`auto_peer.md`](auto_peer.md)。
- 严格 sandbox：`ProtectSystem=strict`、清空 `CapabilityBoundingSet`、`IPAddressAllow=localhost` + `IPAddressDeny=any`（强制仅 loopback 通信）。
- 调试时先 `journalctl -u dn42ctl-server` 查报错，再有针对性地放宽 sandbox 指令。

### dn42ctl-node-agent.service

- 必须以 root 运行（需写 `/etc/bird` 等，调用 `wg` / `ip` / `nmcli`），sandbox 比 server 宽松。
- `Type=exec` + `Restart=always` + `RestartSec=5s`；`[Unit] StartLimitIntervalSec=0` 关掉 systemd 熔断。agent 自身带有指数退避，不应被 systemd 判定为"反复失败"而停掉。
- sandbox 相比原 `node-once.service` 更严格：`UMask=0077`、`MemoryDenyWriteExecute=true`、`ProtectProc=invisible`、`RestrictNamespaces=true`、`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`。
- **刻意不清空 `CapabilityBoundingSet`**：不同于 server，这个进程要对 `/etc/systemd/network/*.netdev` 调 `chown`。若要收窄到 `CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER`，请先在目标环境实测验证。
- **中心主机上**额外设置一个 drop-in（不能放进共享 unit，因为 spoke 上没有 server）：

  ```ini
  # /etc/systemd/system/dn42ctl-node-agent.service.d/hub.conf
  [Unit]
  After=dn42ctl-server.service
  Wants=dn42ctl-server.service
  ```

  没有它 agent 也能工作（退避重连会兜住），只是开机日志会干净一些。

### 开机配置同步保证

原 timer 的 `OnBootSec=2min` 随之删除。作为补偿，agent 在尝试第一次连接之前会先用本地缓存（`/var/lib/dn42ctl/node-cache.sqlite3`）执行一次 `apply()`，因此即使 spoke 重启时 hub 不可达，`/etc/bird` 仍会被渲染。该设计的完整论证见 `docs/architecture/sync_ws_protocol.md`。

## nginx 反代示例

采用三子域名部署（`api.` / `admin.` / `peer.`）。详见 `systemd/nginx.dn42ctl.conf.example`。

核心要点：

- **API 子域名**：反代到 `[::1]:4242`（uvicorn），无静态文件。
- **WebSocket**：节点 agent 的 `/api/v1/nodes/{id}/ws` 需要单独一条**正则** location，透传 `Upgrade` / `Connection` 头并把 `proxy_read_timeout` 放宽到 3600s：

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

  **必须使用正则 location 精确匹配**：兄弟路由 `/api/v1/nodes/{id}/desired|proposals|reports|status` 共享同一前缀，在 `location /` 上统一强制 upgrade 会打断这些路由。nginx 中正则 location 优先于前缀 location，因此 `location /` 保持原样。
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

> 在中心主机上执行 CLI 写命令时建议用 `sudo -u dn42ctl dn42ctl ...`，与 server 进程保持同一个文件 owner，避免 SQLite 文件权限漂移。

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
sudo systemctl restart dn42ctl-server            # 执行 migration + 启动 watcher
# nginx 加上 WS location 后
sudo nginx -t && sudo systemctl reload nginx
```

`dn42ctl node once` / `pull` / `push` / `report` / `status` 这些一次性命令**保留可用**，用于故障排查。

## 升级到 migration v11：全部 node token 必须重签

v11 把 `managed_nodes.api_token_hash` 中所有旧格式的 hash 置为 `NULL`。**所有远程节点的现有 token 立即失效**，它们会拿到 401 并按 `auth_retry_seconds`（默认 300s）退避重试，直到管理员为其重签。

hub 自身的 self 节点不需要人工介入：`dn42ctl serve` 启动时会发现 hash 为 NULL，自动重签并改写 `/etc/dn42ctl/node.toml`。

远程节点逐个处理：

```bash
# 在 hub 上,为每个远程节点重签:
dn42ctl node token rotate <node-id>          # 明文只在这里返回一次

# 把明文写进该节点的 /etc/dn42ctl/node.toml 的 token 字段
# agent 每轮重连都会重读该文件,无需 systemctl restart
```

`dn42ctl node list` 的 `TOKEN` 列会把尚未重签的节点显示为 `no`，可据此确认是否处理完。建议在维护窗口内执行，或先 rotate 再更新文件以缩短各节点的失联时间。

## self node 自动注册

`dn42ctl serve` 启动时自动完成 self 节点注册：执行迁移、生成或读取 `/var/lib/dn42ctl/self_node_id`、UPSERT `managed_nodes` 中 `is_self=1` 的行（同时清零其他行）、在 `/etc/dn42ctl/node.toml` 与库中 hash 不一致时签发 self token，最后监听 `[::1]:4242` 并起 `sync_events` watcher。各步骤的详细语义见 `docs/architecture/sync_hub_spoke.md`。

第一次 `enable --now` 后 self 节点完全就绪，后续 restart 幂等，只要 `node.toml` 与库中 hash 仍然对得上就不会重新生成 token。`--no-self-register` 关闭其中的注册步骤，适用于测试或不希望中心机自管的部署。

## token 轮换

```bash
# admin token: 改 /etc/dn42ctl/server.env 后 systemctl restart dn42ctl-server
# 节点 token 不受影响,但所有正在使用旧 admin token 的请求立即失效

# 节点 token (任意节点):
dn42ctl node token rotate <node-id>     # 打印新 token 明文
# self 节点: /etc/dn42ctl/node.toml 自动同步更新
# 远程节点: 把新 token 安全送达对端,在对端重新执行
#   dn42ctl node init --server ... --node-id ... --token <new>
```

轮换后中心立即用关闭码 `4003` 断开该节点的 WS 连接，agent 进入 300 秒长退避后自动恢复。agent 每轮重连都重读 `node.toml`，因此更新文件即可生效，无需 `systemctl restart`。需要立刻恢复时执行 `sudo systemctl restart dn42ctl-node-agent`。长退避的设计原因见 `docs/architecture/sync_ws_protocol.md`。
