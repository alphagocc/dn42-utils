# 部署：systemd + nginx

dn42ctl 在生产环境以两类 systemd unit 运行：

- `dn42ctl-server.service` —— 中心主机 hub，常驻 API server。
- `dn42ctl-node-once.service` + `dn42ctl-node-once.timer` —— 任何节点（含 self）的同步任务。

unit 模板与 nginx 反代示例位于项目根 `systemd/` 目录。

架构背景见 `docs/architecture/sync_hub_spoke.md`。

## 文件清单

```
systemd/
├── dn42ctl-server.service          # 中心主机:dn42ctl serve 常驻
├── dn42ctl-node-once.service       # 任何节点:跑一次 dn42ctl node once
├── dn42ctl-node-once.timer         # 任何节点:周期触发上面那个 service
└── nginx.dn42ctl.conf.example      # nginx 反代到 [::1]:4242 的示例片段
```

## 设计原则

- **server 不碰系统配置**：`dn42ctl serve` 只读写权威 SQLite 与 self 的 `node.toml`。`/etc/bird` / `/etc/systemd/network` 等渲染目标由 `dn42ctl-node-once.service` 处理。两者职责彻底分离，让 server unit 能用最严的 sandbox。
- **server 只监听 loopback**：TLS / 对外暴露完全交给 nginx。dn42ctl 不接受 `--tls-cert` / `--tls-key`。
- **self 节点不走 nginx**：`node.toml` 中 `server = "http://[::1]:4242"`，直连 uvicorn，不消耗 nginx 连接也不需要再过一次 TLS。
- **node-once 是 oneshot**：失败由 timer 下一轮重试，不在 service 里 `Restart`。

## `dn42ctl-server.service`

```ini
[Unit]
Description=dn42ctl API server (hub)
Documentation=https://github.com/.../dn42-utils
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=dn42ctl
Group=dn42ctl
EnvironmentFile=/etc/dn42ctl/server.env
ExecStart=/usr/local/bin/dn42ctl serve --host ::1 --port 4242
Restart=on-failure
RestartSec=3s

# ---- 文件系统 ----
ReadWritePaths=/var/lib/dn42ctl /etc/dn42ctl
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
UMask=0077

# ---- 内核接口 ----
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible

# ---- 进程能力 ----
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=true
RestrictRealtime=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount @debug @cpu-emulation @obsolete @raw-io @reboot @swap

# ---- 网络 ----
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressAllow=localhost
IPAddressDeny=any

[Install]
WantedBy=multi-user.target
```

### sandbox 指令解释

| 指令 | 目的 |
|------|------|
| `User=dn42ctl` | 非 root 运行，需在系统中预先 `useradd -r -s /usr/sbin/nologin dn42ctl` |
| `ReadWritePaths` | 仅这两个目录可写：权威 SQLite + self node.toml + self_node_id 文件 |
| `ProtectSystem=strict` | `/usr` `/boot` `/etc` 等只读（`/etc/dn42ctl` 被 `ReadWritePaths` 重新打开） |
| `CapabilityBoundingSet=` | 清空所有 capability：纯 userspace HTTP server，不需要 |
| `MemoryDenyWriteExecute` | 阻止 JIT-style RCE |
| `SystemCallFilter` | 只放 `@system-service` 集合，砍掉 mount / debug / raw I/O 等 |
| `IPAddressAllow=localhost` + `IPAddressDeny=any` | 强制只能与 loopback 通信，节点流量先经 nginx，nginx 再 proxy 到 loopback |
| `ProtectProc=invisible` | 进程看不到其他 PID，缩小 `/proc` 攻击面 |
| `RestrictAddressFamilies` | 只留 UNIX + IPv4 + IPv6 socket family |

> 调试时如某条指令阻断了合法行为，先 `systemctl status` + `journalctl -u dn42ctl-server` 查报错，再有针对性地放宽（而不是直接砍掉一长串）。

### server.env

```ini
DN42CTL_API_TOKEN=<admin token,部署时生成,文件 0600 owner=dn42ctl>
```

## `dn42ctl-node-once.service`

```ini
[Unit]
Description=dn42ctl node sync (pull/apply/report)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/dn42ctl node once

# ---- 文件系统 ----
ReadWritePaths=/etc/bird /etc/systemd/network /etc/NetworkManager/system-connections /var/lib/dn42ctl /etc/dn42ctl
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

# ---- 进程能力 ----
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
LockPersonality=true
```

### sandbox 取舍

node-once 必须以 root 跑（需写 `/etc/bird` 等，并调用 `wg` / `ip` / `nmcli`），因此 sandbox 比 server 宽松：

- 不能 `CapabilityBoundingSet=`（`ip link add` 需要 `CAP_NET_ADMIN`）。
- 不能 `RestrictAddressFamilies`（`nmcli` D-Bus 走 `AF_NETLINK`）。
- 不能 `MemoryDenyWriteExecute`（部分 Python C-ext 不兼容）。

但仍开启 `ProtectSystem=strict` + 收窄 `ReadWritePaths`：即便代码出 bug 也不能写到清单之外的路径。

## `dn42ctl-node-once.timer`

```ini
[Unit]
Description=Run dn42ctl node once periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
RandomizedDelaySec=30s
Unit=dn42ctl-node-once.service

[Install]
WantedBy=timers.target
```

- `OnBootSec=2min`：开机后等网络起来再跑。
- `OnUnitActiveSec=10min`：上一次结束后 10 分钟再触发。
- `RandomizedDelaySec=30s`：避免多节点同时打中心。

## nginx 反代示例

`systemd/nginx.dn42ctl.conf.example`：

```nginx
# /etc/nginx/conf.d/dn42ctl.conf
# 仅对外暴露给远程节点 / 管理员; self 节点不经过这里
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name center.example;

    ssl_certificate     /etc/letsencrypt/live/center.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/center.example/privkey.pem;

    # 可选:限制到 dn42 / wireguard 内网
    # allow fd00::/8;
    # deny all;

    # 静态 UI: 公共 auto-peer 页面挂在根路径
    root /var/www/dn42ctl/peer;
    index index.html;

    # 管理后台挂在 /admin/
    location /admin/ {
        alias /var/www/dn42ctl/admin/;
        index index.html;
        try_files $uri $uri/ /admin/index.html;
    }

    # 所有 API 走 loopback uvicorn
    location /api/ {
        proxy_pass         http://[::1]:4242;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # 可选: 对 auto-peer 公共端点限流, 避免被刷
    # 在 http {} 中先声明: limit_req_zone $binary_remote_addr zone=ap:10m rate=10r/m;
    location /api/public/ {
        # limit_req zone=ap burst=20 nodelay;
        proxy_pass         http://[::1]:4242;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

静态资源部署：

```bash
sudo install -d -m 0755 /var/www/dn42ctl
sudo cp -r web/peer  /var/www/dn42ctl/peer
sudo cp -r web/admin /var/www/dn42ctl/admin
```

详细的页面结构与主题策略见 `docs/architecture/web_ui.md`。

## 首次部署流程

### 中心主机

```bash
# 系统用户
sudo useradd -r -s /usr/sbin/nologin dn42ctl
sudo install -d -m 0750 -o dn42ctl -g dn42ctl /var/lib/dn42ctl /etc/dn42ctl

# admin token
sudo install -m 0600 -o dn42ctl -g dn42ctl /dev/stdin /etc/dn42ctl/server.env <<EOF
DN42CTL_API_TOKEN=$(openssl rand -hex 32)
EOF

# server unit
sudo cp systemd/dn42ctl-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-server.service

# 启动后 /etc/dn42ctl/node.toml 已经自动生成,含 self node_id + token

# nginx 反代
sudo cp systemd/nginx.dn42ctl.conf.example /etc/nginx/conf.d/dn42ctl.conf
# 改 server_name / 证书路径
sudo nginx -t && sudo systemctl reload nginx

# self 节点的定时同步
sudo cp systemd/dn42ctl-node-once.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dn42ctl-node-once.timer
```

### 远程被管节点

```bash
# 管理员在中心主机:
dn42ctl node add <new-node-id> --name <hostname>
dn42ctl node token rotate <new-node-id>     # 记下明文 token

# 节点主机:
dn42ctl node init --server https://center.example --node-id <id> --token <token>

# unit
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
