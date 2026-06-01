# 部署：systemd + nginx

dn42ctl 在生产环境以两类 systemd unit 运行：

- `dn42ctl-server.service` —— 中心主机 hub，常驻 API server。
- `dn42ctl-node-once.service` + `dn42ctl-node-once.timer` —— 任何节点（含 self）的同步任务。

unit 模板与 nginx 反代示例位于项目根 `systemd/` 目录。

架构背景见 `docs/architecture/sync_hub_spoke.md`。

## 文件清单

```
systemd/
├── dn42ctl-server.service          # 中心主机: dn42ctl serve 常驻
├── dn42ctl-node-once.service       # 任何节点: 跑一次 dn42ctl node once
├── dn42ctl-node-once.timer         # 任何节点: 周期触发上面那个 service
├── nginx.dn42ctl.conf.example      # nginx 三子域名反代示例
└── server.env.example              # server.env 模板
```

## 设计原则

- **server 不碰系统配置**：`dn42ctl serve` 只读写权威 SQLite 与 self 的 `node.toml`。`/etc/bird` / `/etc/systemd/network` 等渲染目标由 `dn42ctl-node-once.service` 处理。两者职责彻底分离，让 server unit 能用最严的 sandbox。
- **server 只监听 loopback**：TLS / 对外暴露完全交给 nginx。dn42ctl 不接受 `--tls-cert` / `--tls-key`。
- **self 节点不走 nginx**：`node.toml` 中 `server = "http://[::1]:4242"`，直连 uvicorn。
- **node-once 是 oneshot**：失败由 timer 下一轮重试，不在 service 里 `Restart`。

## systemd unit 说明

详细内容见 `systemd/` 目录下的文件，这里只记录关键设计决策。

### dn42ctl-server.service

- 以专用用户 `dn42ctl` 运行（非 root）。
- `EnvironmentFile=/etc/dn42ctl/server.env` 注入 `DN42CTL_API_TOKEN` 和 `DN42CTL_CORS_ORIGINS`。
- 严格 sandbox：`ProtectSystem=strict`、清空 `CapabilityBoundingSet`、`IPAddressAllow=localhost` + `IPAddressDeny=any`（强制仅 loopback 通信）。
- 调试时先 `journalctl -u dn42ctl-server` 查报错，再有针对性地放宽 sandbox 指令。

### dn42ctl-node-once.service

- 必须以 root 运行（需写 `/etc/bird` 等，调用 `wg` / `ip` / `nmcli`），sandbox 比 server 宽松。
- 仍开启 `ProtectSystem=strict` + 收窄 `ReadWritePaths`。

### dn42ctl-node-once.timer

- `OnBootSec=2min`：开机后等网络起来再跑。
- `OnUnitActiveSec=10min`：上一次结束后 10 分钟再触发。
- `RandomizedDelaySec=30s`：避免多节点同时打中心。

## nginx 反代示例

采用三子域名部署（`api.` / `admin.` / `peer.`）。详见 `systemd/nginx.dn42ctl.conf.example`。

核心要点：

- **API 子域名**：反代到 `[::1]:4242`（uvicorn），无静态文件。
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
sudo cp systemd/dn42ctl-node-once.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-server.service
sudo systemctl enable --now dn42ctl-node-once.timer

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

sudo cp systemd/dn42ctl-node-once.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-node-once.timer
```

## self node 自动注册流程

```
systemctl start dn42ctl-server.service
            │
            ▼
   dn42ctl serve 启动
            │
            ▼
   ┌─────────────────────┐
   │ 1. 跑迁移 (含 v5)   │
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
# 若是 self 节点: /etc/dn42ctl/node.toml 自动同步更新
# 若是远程节点: 把新 token 安全送达对端,在对端
#   dn42ctl node init --server ... --node-id ... --token <new>
# 重新覆写 /etc/dn42ctl/node.toml
```
