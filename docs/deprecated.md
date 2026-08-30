# 已废弃 / 已删除的功能

本文件记录已从 dn42ctl 移除的功能，按移除日期倒序排列。用于确认某项功能是否曾经存在、因何移除、替代方案是什么，便于判断旧文档与旧部署环境中的残留配置。

## 2026-08-30

### desired state 的 `paths` 键

中心不再随期望状态下发 `bird_conf_path`、`peers_dir`、`babel_conf_path`、`bird_extra_conf_path`、`networkd_dir`、`nm_dir` 六个写入位置。文件布局是每台机器自己的属性，中心不掌握它，也不应当有能力指定一个 root 常驻进程的写入目标。

替代方案是读本机 `config.toml` 的 `[paths]` 段，与 CLI 的 `genconf` 同源；该文件缺失时落到 `src/dn42ctl/paths.py` 的内置默认值。详见 [`architecture/paths.md`](architecture/paths.md)。

- 使用默认布局的部署**无需任何操作**：被删除的下发值与内置默认值逐字节相同。
- 在中心侧改过 `services/desired_state.py` 的 `DEFAULT_PATHS` 的部署，升级前需要把相同的值写进各节点 `config.toml` 的 `[paths]`，否则文件会迁回默认位置。
- 报文中仍带 `paths` 的缓存（升级过程中的旧快照）不再生效，`node apply` 会为此列一条 warning。
- 该键此前参与内容哈希，移除后每个节点的 digest 变化一次，各收到一次推送、各写一行 `config_revisions`，此后恢复稳定。

### `node.toml` 的 `[apply]` 位置覆盖键

`peers_dir`、`babel_conf_path`、`networkd_dir`、`nm_dir`、`bird_conf_path`、`bird_extra_conf_path`、`config_path` 不再被接受，`[apply]` 段只保留 `reload`。同一台机器的写入位置由 `config.toml` 的 `[paths]` 一处决定，两个文件各说一遍会写出两份 `bird.conf`。

- 这些键留在 `node.toml` 里会让 `load_node_config` 报错，agent 拒绝启动。选择报错而非忽略，是因为被忽略的覆盖会让人以为它仍然生效。
- 迁移方式是把值移进本机 `config.toml` 的 `[paths]`（键名对照：`peers_dir` → `bird_peers_dir`，`babel_conf_path` → `bird_babel_conf`，`nm_dir` → `nm_system_connections_dir`，`bird_conf_path` → `bird_conf`，`bird_extra_conf_path` → `bird_extra_conf`，`networkd_dir` 同名），随后删除 `[apply]` 中的对应行。
- `config_path` 的替代是全局参数 `--config-path` 或环境变量 `DN42CTL_CONFIG`。

## 2026-07-30

### 节点侧轮询同步（`dn42ctl-node-once.timer`）

每 10 分钟跑一次 `dn42ctl node once` 的 systemd timer 已删除，改为常驻 `dn42ctl node agent` + WebSocket 长连接（`dn42ctl-node-agent.service`）。

- CLI 命令 `dn42ctl node once` / `pull` / `push` / `report` / `status` **保留**，用于人工排障。
- 对应的 HTTP 路由也**保留**。
- 详见 [`architecture/sync_ws_protocol.md`](architecture/sync_ws_protocol.md)。

## 2026-07-12

### peer 级 NetworkManager 后端

`bgp peer` / `ibgp peer` 的 `--net nm` 选项与 `.nmconnection` 输出已移除，peer WireGuard 配置统一使用 `systemd-networkd`。

- `dummy_backend` 的 NM 支持**不受影响**。
- 详见 [`architecture/network_backends.md`](architecture/network_backends.md)。

## 2026-05-31

### 旧版 API 路由

`/api/bgp/peers`、`/api/ibgp/peers`、`/api/wg/tunnels`、`/api/genconf` 已被 `/api/admin/*` 下的对应端点取代，详见 [`architecture/rest_api.md`](architecture/rest_api.md)。

### 旧版 NetworkManager inline peers 格式

`scan` 命令不再解析 `peers=` inline 格式，仅支持 `[wireguard-peer.<PUBLIC_KEY>]` section 格式。

### 增量数据库迁移（v1–v7）

合并为单个建表语句（single consolidated migration），详见 [`architecture/database.md`](architecture/database.md)。

### payload 字段兼容默认值

节点间 API 的 `has_wg`、`babel_rxcost`、`babel_type` 字段不再提供缺失时的默认值，缺失时返回 400/422 错误。

> 这确立了本项目**"所有节点运行统一版本"**的既有约定：节点间协议不做版本协商与向后兼容，升级时中心与节点需同步升级。[`architecture/sync_ws_protocol.md`](architecture/sync_ws_protocol.md) 的信封 `v` 字段沿用该约定。

### `create_peer.py`（独立脚本）

功能已被 `dn42ctl bgp peer add` 完全替代。
