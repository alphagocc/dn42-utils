# dn42ctl 规格说明（Spec）

## 目标

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具，核心目标：

- 可复现环境：使用 `uv` 锁定依赖与运行环境。
- CLI 功能：支持 `init`、`bgp peer`、`bgp peer modify`、`ibgp peer`。
- 网络后端：同时支持 `systemd-networkd` 与 `NetworkManager`。
- 强制约束：WireGuard 的 AllowedIPs **必须写入**，但**禁止自动修改路由表**。
- 数据落库：所有状态写入 SQLite，便于多端/多节点集中管理；以 `node_id` 区分节点；`bgp_peers` 与 `ibgp_peers` 分表；结构可扩展，未来可迁移到 Cloudflare D1。
- 可复用 API：业务逻辑与 CLI 解耦，便于未来接入 RESTful API。

## 运行环境与安装

- Python：3.11+（使用标准库 `tomllib` 读取 TOML）。
- 依赖管理：`uv`。

常用命令：

- 创建虚拟环境：`uv venv`
- 安装（可编辑模式）：`uv pip install -e .`
- 运行：`uv run dn42ctl --help`

> `bgp peer`/`ibgp peer` 会调用系统 `wg` 命令生成密钥（见下文），需要安装 wireguard-tools。

## 默认路径与提权

默认情况下会写入系统目录，因此通常需要 root（例如 `sudo`）权限。

- 配置文件：`/etc/dn42ctl/config.toml`（可用 `--config-path` 覆盖）
- SQLite：`/var/lib/dn42ctl/dn42.sqlite3`（可用 `--db-path` 覆盖）
- Bird：
  - 主配置：`/etc/bird/bird.conf`
  - peers 目录：`/etc/bird/peers/`
  - babel：`/etc/bird/babel.conf`
  - ROA v6 include：`/etc/bird/roa_dn42_v6.conf`
- systemd-networkd：`/etc/systemd/network/`
- NetworkManager：`/etc/NetworkManager/system-connections/`

当权限不足时，程序应提示：以 root 运行或通过参数覆盖到可写路径。

## 节点与配置文件（node_id）

- 同一个 SQLite 数据库会管理多台节点。
- 每台节点在 `init` 时生成并写入本地配置文件的 `node_id`（UUIDv4）。
- 数据库中所有表均带 `node_id` 字段进行分区。

## 命令行为

### 1) `dn42ctl init`

用途：初始化本机节点配置与基础 Bird/Babel 配置。

行为：

- 若关键字段缺失，提示用户输入：`OWNAS`、`OWNIPv6`、`OWNNETv6`、`OWNNETSETv6`、`ROUTERID`。
- `OWNIPv6` 允许输入 4 位 hex（作为最后一段），自动扩展为 `fddf:8aef:1053::xxxx`；也接受完整 IPv6。
- 写入本地配置文件（TOML）。
- 初始化/迁移 SQLite（创建表 + `schema_migrations`）。
- 渲染并写入：
  - Bird 主配置（从内置模板渲染、替换 include 路径与 define）。
  - `babel.conf`（初始为空接口列表）。

- 自动配置 ROA v6（用于 Bird 的 `roa_check`）：
  - 若 `bird_roa_v6_conf_path` 不存在：自动从 DN42 ROA 源下载并写入（IPv6 / Bird2 格式）。
  - 安装并启用 systemd 定时更新（Linux/systemd 可用时）：
    - 写入 `dn42-roa-v6.service` / `dn42-roa-v6.timer` 到 `/etc/systemd/system/`。
    - 执行 `systemctl daemon-reload` 与 `systemctl enable --now dn42-roa-v6.timer`。
    - service 内部使用 `curl` 定期刷新 ROA 文件，并在下载后尝试执行 `birdc configure`（失败不应导致 service 失败）。
  - 若当前系统缺少 systemd（或 `systemctl` 不可用）：跳过定时器配置并给出提示。

可通过参数覆盖输出路径：

- `--bird-conf` / `--bird-peers-dir` / `--bird-babel-conf` / `--bird-roa-v6-conf`
- `--networkd-dir` / `--nm-system-connections-dir`

### 2) `dn42ctl bgp peer`

用途：创建一个对外 BGP peer（wireguard 接口 + bird peers + 网络后端配置）。

输入：

- `--asn`、`--pubkey`、`--endpoint`、`--peer-lla`、`--net`（`networkd` 或 `nm`）。
- 缺失时会提示用户输入。

派生规则：

- `ifname`：`dn42_<ASN后4位>`
- `ListenPort`：`ASN后5位`（超出范围则报错）

WireGuard：

- 本端密钥：必须调用系统命令生成：`wg genkey` 与 `wg pubkey`。
- 本端 LLA：随机生成 `fe80::xxxx:xxxx/64`。
- AllowedIPs：默认写入 `fe80::/64` 与 `fd00::/8`（同时见“禁止修改路由表”约束）。

输出：

- Bird peer conf：写入到 `bird_peers_dir/<ifname>.conf`。
- networkd：写入 `<ifname>.netdev` 与 `<ifname>.network`。
- NetworkManager：写入 `<ifname>.nmconnection`（文件权限目标为 0600）。
- CLI 会展示必要信息（本端公钥、本端 LLA、ListenPort、写入的文件路径）。

### 3) `dn42ctl bgp peer modify`

用途：当 peer 信息无法一次性填写完整时，读取数据库中已有记录并根据新输入重新生成配置文件。

行为：

- 读取数据库中该 peer 的现有记录，提示用户输入缺失或需要更新的字段。
- 更新数据库记录。
- 重新渲染并覆盖生成 Bird peer conf 与对应网络后端配置文件。

### 4) `dn42ctl ibgp peer`

用途：创建内网 iBGP peer（wireguard 隧道 + bird peers + babel 互联）。

输入：

- `--name`、`--pubkey`、`--endpoint`、`--peer-lla`、`--net`。

派生规则：

- `ifname`：`wg_<sanitize(name)>`（长度不得超过 15）。
- `ListenPort`：从高端口随机选择且避免与当前节点已有端口冲突。

输出：

- Bird iBGP peer conf：写入 `bird_peers_dir/ibgp_<name>.conf`。
- 写入 networkd 或 NetworkManager 的 wireguard 配置文件。
- **重生成** `babel.conf`：从数据库读取该节点所有 iBGP peer 的接口列表，确定性、幂等地生成。

### 5) `dn42ctl show`

用途：展示当前节点（`node_id`）的配置状态，便于巡检与排障。

子命令：

- `dn42ctl show wg`：展示 WireGuard 维度信息（包含 BGP 与 iBGP 的隧道）。
- `dn42ctl show bgp`：展示外部 BGP peers（按 `peer_asn`）。
- `dn42ctl show ibgp`：展示 iBGP peers（按 `name`）。
- `dn42ctl show all`：汇总展示所有信息。

数据来源：

- 主来源：SQLite（dn42ctl 管理的 peers）。
- 额外：尽力附带“实时状态”（若系统命令可用）：
  - `wg show`：显示握手/流量等运行态信息（按接口名）。
  - `birdc`：显示 BGP protocol 运行态（按协议名）。
  - 若命令不可用或失败：不应导致 show 失败，只提示该部分不可用。

输出格式：

- 默认：人类可读文本。
- `--json`：输出结构化 JSON（便于未来 RESTful API 复用）。

### 6) `dn42ctl del peer`

用途：删除某个 peer（同时清理可推断的生成文件）。

子命令：

- `dn42ctl del peer bgp <ASN>`：删除外部 BGP peer。
- `dn42ctl del peer ibgp <name>`：删除 iBGP peer。

行为：

- 删除前必须二次确认（交互 prompt）。
- 删除数据库记录。
- 删除生成文件（按记录与配置路径推断）：
  - Bird peer conf（`bird_peers_dir/*.conf`）。
  - networkd：`<ifname>.netdev` 与 `<ifname>.network`。
  - NetworkManager：`<ifname>.nmconnection`。
- 若删除的是 iBGP peer：删除后必须从 DB 幂等重生成 `babel.conf`。

### 7) `dn42ctl scan`

用途：扫描本地已有配置并导入 SQLite，便于“接管”已有环境。

默认扫描范围（若目录存在则扫描）：

- Bird: `/etc/bird` 与 `/etc/bird6`（主要读取 peers 片段目录）。
- WireGuard: `/etc/wireguard`（wg-quick 配置）。
- systemd-networkd: `/etc/systemd/network`。
- NetworkManager: `/etc/NetworkManager/system-connections`。

默认导入规则：

- 仅导入接口名符合 dn42ctl 约定的 peer：
  - BGP：`dn42_####`
  - iBGP：`wg_<name>`
- 尽力从 networkd/NM/wg-quick/Bird peers 中拼装出同一个 peer 的字段（例如 endpoint/keys/AllowedIPs/peer_lla 等）；缺失字段允许为空。
- 冲突处理（DB 已存在同名 peer）：应提示用户手动处理（默认跳过，不静默覆盖）。

实现约束：

- 为了写入 DB 的 `wg_public_key`，当仅能获取 `wg_private_key` 时，需要调用系统 `wg pubkey` 计算公钥；若缺少 `wg` 命令，应提示安装 wireguard-tools。

## 网络后端细节

### systemd-networkd

- `.netdev` 使用 `Kind=wireguard`。
- 必须设置：`RouteTable=off`（强制约束：禁止因 AllowedIPs 自动修改路由表）。
- `.network` 为接口配置 LLA 地址，并设置 `Peer=<peer_lla>`。

### NetworkManager

- 使用 keyfile 格式（`.nmconnection`），`type=wireguard`。
- 必须设置：`[wireguard] peer-routes=false`（强制约束：禁止因 AllowedIPs 自动修改路由表）。
- `peers=` 采用 NetworkManager 的 wireguard peers 语法，`allowed-ips` 多 CIDR 使用 `;` 分隔。
- `connection.uuid` 需要稳定：基于 `node_id + ifname` 生成确定性 UUIDv5，避免“重新生成导致新连接”的问题。

## “禁止修改路由表”约束

- AllowedIPs 仍会写入（便于对端/配置完整性）。
- 但必须显式关闭自动路由：
  - networkd：`RouteTable=off`
  - NetworkManager：`peer-routes=false`

本工具不负责自动添加任何 DN42 路由；如需路由策略，请由用户在系统层面自行管理。

## SQLite 数据结构（v1）

- `schema_migrations(version)`：迁移版本。
- `nodes(node_id, created_at, updated_at)`：节点表。
- `bgp_peers`：外部 BGP peer，包含（节选）：
  - `node_id`、`peer_asn`、`ifname`
  - `wg_private_key`、`wg_public_key`
  - `peer_public_key`、`endpoint`
  - `local_lla`、`peer_lla`
  - `listen_port`、`allowed_ips_json`、`net_backend`
  - `created_at`、`updated_at`
- `ibgp_peers`：内网 iBGP peer，字段与 `bgp_peers` 类似，额外包含 `name`。

约束：

- `(node_id, peer_asn)`、`(node_id, ifname)` 唯一。
- iBGP `(node_id, name)`、`(node_id, ifname)` 唯一。

## API 分层

- CLI 层：负责参数解析与交互提示。
- Service 层：对外暴露可复用函数（例如 `init_node/create_bgp_peer/modify_bgp_peer/create_ibgp_peer`），未来 REST API 可直接复用。
- 新增的 show/del/scan 同样必须以 service 函数对外暴露（返回结构化数据），CLI 仅负责格式化输出与交互（prompt/confirm）。
- Render 层：纯文本渲染（Bird/Babel/networkd/NM），确保可测试与幂等。
- DB 层：SQLite + migrations。

## 安全性说明

- SQLite 会保存 WireGuard 私钥（用于多端/未来集中管理），请确保数据库文件权限与备份策略。
- 配置文件与 NetworkManager 连接文件目标权限为 0600（尽力设置；不同平台可能行为不同）。
