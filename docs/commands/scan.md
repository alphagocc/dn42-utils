# 命令：scan

## `dn42ctl scan`

用途：扫描本地已有配置并导入 SQLite，便于“接管”已有环境。

## 扫描范围

默认扫描范围（若目录存在则扫描）：

- Bird：`/etc/bird` 与 `/etc/bird6`（主要读取 peers 片段目录）。
- systemd-networkd：`/etc/systemd/network`。
- NetworkManager：`/etc/NetworkManager/system-connections`。

不支持（已明确移除）：

- wg-quick：`/etc/wireguard`。

## bird.conf 识别与 paths 自动修正

扫描开始前，尽力读取当前系统的 Bird 主配置文件并识别 include 路径，用于修正本地 `config.toml` 的 `[paths]`：

- Bird 主配置候选路径：优先使用 `config.paths.bird_conf`，若不存在/不可读则尝试 `/etc/bird/bird.conf` 与 `/etc/bird.conf`。
- 从 bird.conf 中识别：
  - peers include（推导 `bird_peers_dir`）
  - `babel.conf` include（推导 `bird_babel_conf_path`）
  - ROA v6 include（推导 `bird_roa_v6_conf_path`）
- 若识别到的路径与本地 `config.toml` 不一致：自动回写到 `config.toml` 后再继续 scan。

## 默认导入规则

- 仅导入接口名符合 dn42ctl 约定的 peer：
  - BGP：`dn42_####`
  - iBGP：`wg_<name>`
- iBGP peer 的 `peer_ip` 从 Bird peer conf 中提取：匹配 `neighbor <ip> as OWNAS;` 模式（支持带/不带 `%ifname` 后缀的两种格式）。若 Bird conf 不存在或无法解析，`peer_ip` 为空，后续 `genconf --all` 将跳过该 peer 的 Bird conf 生成并发出警告。
- 尽力读取 `babel.conf` 并解析每个 `interface` 的 `rxcost`：
  - 若能匹配到 `wg_<name>` 接口：写入 DB 的 `ibgp_peers.babel_rxcost`。
  - 若无法读取/解析或缺失该接口：记录 warning，并回退到默认值（保持兼容）。
- 尽力从 networkd/NM/Bird peers 中拼装出同一个 peer 的字段（例如 endpoint/keys/AllowedIPs/peer_lla 等）；缺失字段允许为空。
- `ListenPort` 可能缺失（例如仅出站连接、位于防火墙/NAT 后的场景）；scan 应允许缺失并以”未设置”状态入库（例如使用 0 作为哨兵值），后续重生成配置时应省略对应字段。
- 冲突处理（DB 已存在同名 peer）：应提示用户手动处理（默认跳过，不静默覆盖）。

## 错误恢复

- 当扫描大量文件时，如果某单个文件格式错误导致解析异常：
  - 应将该文件记录到 `skipped`/`warnings` 列表
  - CLI 默认输出“文件路径 + 简短错误信息”
  - 不应抛出致命错误中断整体 scan
