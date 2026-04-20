# 命令：init

## `dn42ctl init`

用途：初始化本机节点配置（`config.toml`）与 SQLite 基础结构。

### 行为

- 若关键字段缺失，提示用户输入：
  - `OWNAS`、`OWNIPv6`、`OWNNETv6`、`OWNNETSETv6`、`ROUTERID`
- `ROUTERID`：
  - 默认值应为随机生成的 `169.254.X.Y`（`X/Y` 为 1-254）
  - 写入本地配置文件以保持稳定（后续重跑 init 不应变化）
- `OWNIPv6`：
  - 允许输入 4 位 hex（作为最后一段），自动扩展为 `fddf:8aef:1053::xxxx`
  - 也接受完整 IPv6
- 写入本地配置文件（TOML）。
- 初始化/迁移 SQLite（创建表 + `schema_migrations`）。

默认情况下，`init` **不生成** Bird/Babel/ROA/systemd timer 等配置文件；需要显式运行 `dn42ctl genconf`，或在 init 时使用 `--genconf`。

### 路径覆盖参数

- `--bird-conf` / `--bird-peers-dir` / `--bird-babel-conf` / `--bird-roa-v6-conf`
- `--networkd-dir` / `--nm-system-connections-dir`

### 生成配置开关

- `--genconf/--no-genconf`：是否在 init 完成后立即生成配置文件（默认 `--no-genconf`）。
