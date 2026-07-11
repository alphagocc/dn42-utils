# 命令：genconf

## `dn42ctl genconf`

用途：根据本地配置文件与 SQLite 状态，生成/刷新 Bird/Babel/ROA 等配置文件（可重复运行，尽量幂等）。

### 选项

- `--all`：同时重新生成所有 peers 的 Bird peer 配置和 WireGuard networkd 配置（`.netdev`/`.network`）。从 SQLite 遍历所有 BGP 和 iBGP peers，适用于配置文件丢失后批量恢复。生成完毕后执行一次 `networkctl reload`。

### 行为

- 渲染并写入：
  - Bird 主配置（从内置模板渲染、替换 include 路径与 define）。
  - `babel.conf`：从数据库读取该节点所有 iBGP peer 的接口列表与各自的 `rxcost`，确定性、幂等地生成。
- 确保 Bird peers 目录存在（若不存在则创建）。
- 当指定 `--all` 时，额外生成：
  - 每个 BGP peer 的 Bird peer conf（`{bird_peers_dir}/{ifname}.conf`）和 WG 配置。
  - 每个 iBGP peer 的 Bird peer conf（`{bird_peers_dir}/ibgp_{name}.conf`）和 WG 配置（若有 WG 隧道）。

### ROA v6（Bird `roa_check`）

- 若 `bird_roa_v6_conf_path` 不存在：自动从 DN42 ROA 源下载并写入（IPv6 / Bird2 格式）。
- 下载失败时写入占位文件并输出警告（不阻断后续流程）。
- genconf **不安装** systemd 定时器。如需自动定时更新 ROA 文件，请使用 `dn42ctl system install roa-service`。
