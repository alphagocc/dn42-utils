# dn42ctl

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具，支持多节点中心化管理。

核心特性：

- 业务逻辑与 CLI 解耦（Service 层可复用，REST API + Web UI 可选启用）。
- 同时支持 `systemd-networkd` 与 `NetworkManager` 两种网络后端。
- **强制约束**：WireGuard 的 `AllowedIPs` 必须写入，但工具 **禁止自动修改路由表**。
- 所有状态写入 SQLite；用 `node_id` 区分节点，便于多节点集中管理。
- **Hub-Spoke 多节点同步**：中心节点运行 `dn42ctl serve`，远程节点通过 API 拉取配置并自动应用。
- **Auto-peer**：公共 Web 向导，持有合法 dn42 ASN 的用户可通过 SSH/PGP 签名验证身份后提交 peering 请求。

> 详细规格见 `docs/spec.md`。

## 安装与运行

- Python 3.11+，推荐使用 `uv`
- `bgp peer` / `ibgp peer` / `scan` 需要系统命令 `wg`（wireguard-tools）

```bash
uv venv && uv pip install -e .
uv run dn42ctl --help
```

## 单机使用

### 初始化

```bash
sudo uv run dn42ctl init           # 仅初始化 config + DB
sudo uv run dn42ctl init --genconf # 同时生成 Bird/Babel/ROA 配置
```

### 生成/刷新配置

```bash
sudo uv run dn42ctl genconf
```

### 创建 BGP peer

```bash
sudo uv run dn42ctl bgp peer --asn 424242xxxx --pubkey <PEER_PUBKEY> --endpoint <HOST:PORT> --peer-lla <fe80::...> --net networkd
```

### 创建 iBGP peer

```bash
sudo uv run dn42ctl ibgp peer --name <NAME> --pubkey <PEER_PUBKEY> --endpoint <HOST:PORT> --peer-lla <fe80::...> --net nm
```

### 扫描并导入现有配置

```bash
sudo uv run dn42ctl scan
```

### 巡检与删除

```bash
dn42ctl show --help
dn42ctl del --help
```

## REST API 与 Web UI

```bash
sudo DN42CTL_API_TOKEN=<token> uv run dn42ctl serve --host ::1 --port 4242
```

- 默认绑定 `[::1]:4242`，不处理 TLS（由 nginx 反代承担）。
- 启动时自动注册 self 节点（`--no-self-register` 可关闭）。
- 三类鉴权主体：**admin**（全局管理）、**node**（节点同步）、**peer-session**（auto-peer 向导）。

`web/` 目录下包含两个纯静态站点（HTML + Vanilla JS + Tailwind CDN）：

- **`web/admin/`** — 管理后台：节点/peer 管理、提案审批、配置快照回滚。
- **`web/peer/`** — 公共 auto-peer 向导：4 步完成 peering 请求。

> 路由表见 `docs/architecture/rest_api.md`，部署见 `docs/architecture/deployment.md`。

## 多节点管理（Hub-Spoke）

中心节点运行 `dn42ctl serve`，远程节点通过 API 同步：

```bash
# 中心侧
dn42ctl node add --name node-a
dn42ctl node token rotate <node-id>

# 节点侧
sudo dn42ctl node init --server https://hub.example.com --node-id <id> --token <token>
sudo dn42ctl node once   # pull → apply → report
```

支持 pull/apply、push/scan、提案审批、配置快照回滚、token 与写策略管理。

> 详见 `docs/architecture/sync_hub_spoke.md` 与 `docs/commands/node.md`。

## 文档

- 规格说明：`docs/spec.md`
- 架构文档：`docs/architecture/`
- 命令参考：`docs/commands/`
