# 命令：show

## `dn42ctl show`

用途：展示当前节点（`node_id`）的配置状态，便于巡检与排障。

**不带子命令时，等价于 `dn42ctl show all`。**

### 子命令

- `dn42ctl show wg`：展示 WireGuard 维度信息（包含 BGP 与 iBGP 的隧道；无 WG 的 iBGP peer 不纳入）。
- `dn42ctl show bgp`：展示外部 BGP peers（按 `peer_asn`）。
- `dn42ctl show ibgp`：展示 iBGP peers（按 `name`）。
- `dn42ctl show all`：汇总展示所有信息。

### 数据来源

- 主来源：SQLite（dn42ctl 管理的 peers）。
- 额外：尽力附带"实时状态"（若系统命令可用）：
  - `wg show`：显示握手/流量等运行态信息（按接口名）。
  - `birdc`：显示 BGP protocol 运行态（按协议名）。

若外部命令不可用或失败：不应导致 `show` 失败，只提示该部分不可用。

### 输出格式

- 默认：人类可读文本。
- `--json`：输出结构化 JSON（便于未来 RESTful API 复用）。
