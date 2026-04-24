# dn42ctl 规格说明（Index）

本文件是 `dn42ctl` 的“总索引（Index）”。详细的命令与架构规范拆分在 `docs/commands/` 与 `docs/architecture/`。

## 目标

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具，核心目标：

- 可复现环境：使用 `uv` 锁定依赖与运行环境。
- CLI 功能：支持 `init`、`genconf`、`bgp peer [add|modify|del]`、`ibgp peer [add|modify|del]`、`show`、`scan`。
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

> `bgp peer`/`ibgp peer`/`scan` 会调用系统 `wg` 命令（生成密钥或从私钥推导公钥），需要安装 wireguard-tools。

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
- 渲染引擎使用 Jinja2；验收以“语义一致”为准（允许空白差异）。

## Babel rxcost 设计

- `babel.conf` 的 `rxcost` 按 **iBGP peer 粒度**配置并存储在 SQLite（`ibgp_peers.babel_rxcost`）。
- 创建 iBGP peer 时必须提供 `rxcost`（命令行参数或交互提示）——仅在有 WG 隧道时需要。
- 修改 iBGP peer 的 `rxcost` 后应重生成 `babel.conf`（幂等）。
- `scan` 会从现有 `babel.conf` 中尽力解析各接口的 `rxcost` 并导入 SQLite；解析失败会给出 warning 并回退到默认值（保持原来行为）。

## iBGP 互联设计

- iBGP peer 使用**网内 IP**（`peer_ip`）作为 Bird neighbor 地址，而非 link-local 地址（LLA）。这是因为 iBGP 内网有 babel 路由协议，无需依赖 LLA 互联。
- iBGP peer 支持**无 WireGuard 模式**（`--no-wg`）：仅生成 Bird peer conf，不创建 WG 隧道、不修改 babel.conf。适用于对端已通过其他方式（如物理网络、已有隧道）可达的场景。
- iBGP peer 的 `endpoint` 为可选：对端可能在防火墙后，无需填写。
- iBGP WireGuard 隧道的 `AllowedIPs` 固定为 `::/0`：iBGP 对端均为可信任的自有机器，需要放行全部流量以支持 babel 路由协议等互联需求。BGP（eBGP）peer 仍使用受限的 `AllowedIPs`（`fe80::/64, fd00::/8`）。

## dn42-dummy 接口

- `init` 和 `genconf` 均会尝试创建 `dn42-dummy` 接口并绑定 `OWNIPv6/128` 地址（幂等操作）。
- 若接口已存在且地址已绑定，则跳过。
- 网络管理方式自动检测：
  - 若 `nmcli` 命令存在 **且** NetworkManager 服务正在运行（`nmcli general status` 返回 0），使用 NM 方式创建。
  - 否则使用 `iproute2`（`ip link add` / `ip addr add`）。
- NM 命令：`nmcli connection add type dummy ifname dn42-dummy ipv6.method manual ipv6.addresses <OWNIPv6>/128`
- iproute2 命令：`ip link add dn42-dummy type dummy` + `ip addr add <OWNIPv6>/128 dev dn42-dummy` + `ip link set dn42-dummy up`
- 创建失败不应阻断 init/genconf 流程，仅输出警告。

## Babel interface type 设计

- `babel.conf` 的 `type` 按 **iBGP peer 粒度**配置并存储在 SQLite（`ibgp_peers.babel_type`）。
- 取值范围：`wired`、`wireless`、`tunnel`。默认值为 `tunnel`。
- 创建 iBGP peer 时可通过 `--type` 指定（默认 `tunnel`）。
- 修改 iBGP peer 时可通过 `--type` 修改。
- `scan` 会从现有 `babel.conf` 中尽力解析各接口的 `type` 并导入 SQLite；解析失败回退到默认值 `tunnel`。

## 详细规范索引

### 架构

- 数据库：`docs/architecture/database.md`
- 网络后端：`docs/architecture/network_backends.md`

### 命令

- init：`docs/commands/init.md`
- genconf：`docs/commands/genconf.md`
- bgp peer (add/modify/del)：`docs/commands/bgp_peer.md`
- ibgp peer (add/modify/del)：`docs/commands/ibgp_peer.md`
- show / peer del：`docs/commands/show_and_del.md`
- scan：`docs/commands/scan.md`
