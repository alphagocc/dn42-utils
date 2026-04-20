# dn42ctl 规格说明（Spec）

## 目标

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具，核心目标：

- 可复现环境：使用 `uv` 锁定依赖与运行环境。
- CLI 功能：支持 `init`、`bgp peer`、`bgp peer modify`、`ibgp peer`。
- CLI 功能：支持 `init`、`genconf`、`bgp peer`、`bgp peer modify`、`ibgp peer`。
- 网络后端：同时支持 `systemd-networkd` 与 `NetworkManager`。
- 强制约束：WireGuard 的 AllowedIPs **必须写入**，但**禁止自动修改路由表**。
- 数据落库：所有状态写入 SQLite，便于多端/多节点集中管理；以 `node_id` 区分节点；`bgp_peers` 与 `ibgp_peers` 分表；结构可扩展，未来可迁移到 Cloudflare D1。
- 可复用 API：业务逻辑与 CLI 解耦，便于未来接入 RESTful API。

## 运行环境与安装

- Python：3.11+（使用标准库 `tomllib` 读取 TOML）。
- 依赖管理：`uv`。

# dn42ctl 规格说明（Index）

本文件是 `dn42ctl` 的“总索引（Index）”。详细的命令与架构规范已拆分到 `docs/commands/` 与 `docs/architecture/`。

## 目标

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具，核心目标：

- 可复现环境：使用 `uv` 锁定依赖与运行环境。
- CLI 功能：支持 `init`、`genconf`、`bgp peer`、`bgp peer modify`、`ibgp peer`、`show`、`del peer`、`scan`。
- 网络后端：同时支持 `systemd-networkd` 与 `NetworkManager`。
- 强制约束：WireGuard 的 AllowedIPs **必须写入**，但**禁止自动修改路由表**。
- 数据落库：所有状态写入 SQLite，便于多端/多节点集中管理；以 `node_id` 区分节点。
- 分层：CLI/Service/Render/DB 解耦，便于未来接入 RESTful API。

## 运行环境与安装

- Python：3.11+（使用标准库 `tomllib` 读取 TOML）。
- 依赖管理：`uv`。

常用命令：

- 创建虚拟环境：`uv venv`
- 安装（可编辑模式）：`uv pip install -e .`
- 运行：`uv run dn42ctl --help`

> `bgp peer`/`ibgp peer` 会调用系统 `wg` 命令生成密钥，需要安装 wireguard-tools。

## 默认路径与提权

默认情况下会写入系统目录，因此通常需要 root（例如 `sudo`）权限。

- 配置文件：`/etc/dn42ctl/config.toml`（可用 `--config-path` 覆盖）
- SQLite：`/var/lib/dn42ctl/dn42.sqlite3`（可用 `--db-path` 覆盖）
- Bird：
  - 主配置：`/etc/bird/bird.conf`（部分发行版也可能是 `/etc/bird.conf`）
  - peers 目录：`/etc/bird/peers/`
  - babel：`/etc/bird/babel.conf`
  - ROA v6 include：`/etc/bird/roa_dn42_v6.conf`
- systemd-networkd：`/etc/systemd/network/`
- NetworkManager：`/etc/NetworkManager/system-connections/`

当权限不足时，程序应提示：以 root 运行或通过参数覆盖到可写路径。

## 核心设计约束（必须保持）

- 禁止自动改路由表：
  - networkd：`RouteTable=off`
  - NetworkManager：`peer-routes=false`
- `scan` 仅支持 `systemd-networkd` 与 `NetworkManager`（不支持 wg-quick `/etc/wireguard` 扫描）。
- 渲染引擎将升级为 Jinja2；验收以“语义一致”为准（允许空白差异）。

## 详细规范索引

### 架构

- 数据库：`docs/architecture/database.md`
- 网络后端：`docs/architecture/network_backends.md`

### 命令

- init：`docs/commands/init.md`
- genconf：`docs/commands/genconf.md`
- bgp peer：`docs/commands/bgp_peer.md`
- ibgp peer：`docs/commands/ibgp_peer.md`
- show / del：`docs/commands/show_and_del.md`
- scan：`docs/commands/scan.md`
  - 安装并启用 systemd 定时更新（Linux/systemd 可用时）：
