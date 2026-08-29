# dn42ctl 规格说明（Index）

本文件是 `dn42ctl` 的**总索引**，只保留全局目标与必须长期保持的核心约束。详细的命令与架构规范拆分在 [`docs/commands/`](commands/) 与 [`docs/architecture/`](architecture/)。

> 新增功能时：为其创建专门的文档文件，完整规格写入该文件，并在本文件中添加**一行**引用。

## 目标

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具，核心目标：

- 可复现环境：使用 `uv` 锁定依赖与运行环境。
- CLI 功能：`init`、`genconf`、`bgp peer [add|modify|del]`、`ibgp peer [add|modify|del]`、`show`、`scan`、`serve`、`node`、`system`、`deploy`。
- 网络后端：peer WireGuard 配置仅支持 `systemd-networkd`；`dummy_backend` 仍支持 `networkd` 与 `nm`。
- 强制约束：WireGuard 的 AllowedIPs **必须写入**，但**禁止自动修改路由表**。
- 数据持久化：所有状态写入 SQLite，便于多端/多节点集中管理；以 `node_id` 区分节点。
- 多节点中心化同步：hub-spoke 架构；节点侧常驻 agent 通过 WebSocket 长连接接收中心推送。
- 分层：CLI / Service / Render / DB 解耦，Service 层可被 REST API 直接复用。

## 核心设计约束（必须保持）

这些约束是本项目的身份，修改前必须有充分理由：

- **禁止自动改路由表**：`AllowedIPs` 必须写入以保证配置完整性，但工具不得因此改动系统路由表。
  - networkd：`RouteTable=off`
  - NetworkManager（仅 dummy_backend）：`peer-routes=false`
  - 工具不负责添加任何 DN42 路由策略；如需路由，由用户在系统层面自行管理。
- **`scan` 仅支持 `systemd-networkd`**：不支持 wg-quick（`/etc/wireguard`）或 NetworkManager 扫描。
- **渲染引擎使用 Jinja2**，且启用 `StrictUndefined`，因此缺失上下文变量应视为 bug。验收以"语义一致"为准（允许空白差异）。
- **所有节点运行统一版本**：节点间协议不做版本协商与向后兼容，中心与节点需同步升级。

## 运行环境

- Python 3.11+（使用标准库 `tomllib` 读取 TOML），依赖管理使用 `uv`。
- `bgp peer` / `ibgp peer` / `scan` 会调用系统 `wg` 命令，需要安装 wireguard-tools。

> 安装与上手步骤见 [`../README.md`](../README.md)；默认路径与提权要求见 [`architecture/paths.md`](architecture/paths.md)。

## 详细规范索引

### 架构

| 文档 | 内容 |
| --- | --- |
| [`architecture/paths.md`](architecture/paths.md) | 默认路径与提权 |
| [`architecture/database.md`](architecture/database.md) | 数据库 |
| [`architecture/db_browse.md`](architecture/db_browse.md) | 数据库只读浏览（白名单与脱敏） |
| [`architecture/network_backends.md`](architecture/network_backends.md) | 网络后端（networkd / NetworkManager） |
| [`architecture/babel.md`](architecture/babel.md) | Babel 配置生成（rxcost / interface type） |
| [`architecture/rest_api.md`](architecture/rest_api.md) | REST API 路由表 |
| [`architecture/sync_hub_spoke.md`](architecture/sync_hub_spoke.md) | 多节点中心化同步 |
| [`architecture/node_addressing.md`](architecture/node_addressing.md) | 节点地址集中管理（传播、下发、reload） |
| [`architecture/sync_ws_protocol.md`](architecture/sync_ws_protocol.md) | 节点同步 WebSocket 协议 |
| [`architecture/deployment.md`](architecture/deployment.md) | 部署（systemd + nginx） |
| [`architecture/validation.md`](architecture/validation.md) | 输入校验 |
| [`architecture/testing.md`](architecture/testing.md) | 测试基础设施 |
| [`architecture/language_check.md`](architecture/language_check.md) | 中文语言规范的提交前校验 |
| [`architecture/web_ui.md`](architecture/web_ui.md) | Web UI（admin + peer，React + Vite） |
| [`architecture/auto_peer.md`](architecture/auto_peer.md) | Auto-peer 公共 API |

### 命令

| 文档 | 命令 |
| --- | --- |
| [`commands/init.md`](commands/init.md) | `init`（含 dn42-dummy 接口） |
| [`commands/genconf.md`](commands/genconf.md) | `genconf` |
| [`commands/bgp_peer.md`](commands/bgp_peer.md) | `bgp peer [add\|modify\|del]` |
| [`commands/ibgp_peer.md`](commands/ibgp_peer.md) | `ibgp peer [add\|modify\|del]` |
| [`commands/show.md`](commands/show.md) | `show` |
| [`commands/scan.md`](commands/scan.md) | `scan` |
| [`commands/node.md`](commands/node.md) | `node`（admin + 节点同步） |
| [`commands/system.md`](commands/system.md) | `system`（系统组件安装/卸载） |
| [`commands/web.md`](commands/web.md) | `deploy`（web / daemon 部署） |

### 其他

[`deprecated.md`](deprecated.md) 记录已废弃、已删除的功能及其替代品。

[`reviews/`](reviews/) 存放历史代码审计报告。每份报告均为特定时间点的快照，其中的判断应以报告所对应的 commit 为准，当前代码状态需另行核实。
