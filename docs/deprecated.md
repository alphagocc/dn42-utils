# 已废弃 / 已删除的功能

本文件记录已从 dn42ctl 移除的功能，按移除日期倒序排列。用于确认某项功能是否曾经存在、因何移除、替代方案是什么，便于判断旧文档与旧部署环境中的残留配置。

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
