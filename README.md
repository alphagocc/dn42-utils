# dn42ctl

`dn42ctl` 是一个用于生成/维护 DN42 相关配置的 Python CLI 工具。

核心特性：

- 业务逻辑与 CLI 解耦（Service 层可复用，便于未来接入 REST API）。
- 同时支持 `systemd-networkd` 与 `NetworkManager` 两种网络后端。
- **强制约束**：WireGuard 的 `AllowedIPs` 必须写入，但工具 **禁止自动修改路由表**：
  - networkd：`RouteTable=off`
  - NetworkManager：`peer-routes=false`
- 所有状态写入 SQLite；用 `node_id` 区分节点，便于多节点集中管理。

> 详细规格见 `docs/spec.md`。

## 运行环境

- Python 3.11+
- 推荐使用 `uv` 管理环境与运行（项目依赖已在 `pyproject.toml` 中声明）
- `bgp peer` / `ibgp peer` / `scan` 需要系统命令 `wg`（wireguard-tools）

## 安装与运行（推荐 uv）

在项目根目录：

```bash
uv venv
uv pip install -e .
uv run dn42ctl --help
```

## 快速上手

### 1) 初始化（写 config + 初始化 DB）

默认仅初始化本机配置与 SQLite，不生成 Bird/Babel/ROA 文件：

```bash
sudo uv run dn42ctl init
```

- 首次初始化会生成 `node_id`（UUIDv4）并写入 config。
- `ROUTERID` 默认值会随机生成 `169.254.X.Y`（`X/Y` 为 1-254），并写入 config 以保持稳定。

如需 init 后立刻生成配置：

```bash
sudo uv run dn42ctl init --genconf
```

可用 `--config-path` / `--db-path` 或 init 的路径参数覆盖默认系统路径（例如在非 root 权限场景）。

### 2) 生成/刷新配置（Bird/Babel/ROA）

```bash
sudo uv run dn42ctl genconf
```

`genconf` 会：

- 渲染并写入 Bird 主配置（`bird.conf`）
- 幂等生成 `babel.conf`（从 DB 读取该节点所有 iBGP 接口列表）
- 确保 Bird peers 目录存在
- 若缺失则下载 ROA v6 文件，并在 Linux + systemd 可用时安装/启用定时更新

### 3) 创建 BGP peer（对外）

```bash
sudo uv run dn42ctl bgp peer --asn 424242xxxx --pubkey <PEER_PUBKEY> --endpoint <HOST:PORT> --peer-lla <fe80::...> --net networkd
```

- 若参数缺失会进入交互提示。
- 可选：`--listen-port 0` 表示不设置 ListenPort（适用于仅出站、位于防火墙/NAT 后的场景）。
- `AllowedIPs` 默认包含 `fe80::/64` 与 `fd00::/8`，但不会自动添加路由。

### 4) 修改 BGP peer（重生成配置）

```bash
sudo uv run dn42ctl bgp peer modify 424242xxxx
```

读取 DB 中已有记录并根据新输入重生成相关配置文件。

### 5) 创建 iBGP peer（内网）

```bash
sudo uv run dn42ctl ibgp peer --name <NAME> --pubkey <PEER_PUBKEY> --endpoint <HOST:PORT> --peer-lla <fe80::...> --net nm
```

- `ifname` 为 `wg_<sanitize(name)>`（长度 ≤ 15）。
- `ListenPort` 自动从高端口选择并避免冲突。
- 可选：`--listen-port 0` 表示不设置 ListenPort。
- 会在写入 peer 配置后 **幂等重生成** `babel.conf`。

### 6) 扫描本机已有配置并导入 DB（接管现有环境）

```bash
sudo uv run dn42ctl scan
```

- 会尽力读取 Bird 主配置（优先使用 config 里的 `paths.bird_conf`，否则尝试 `/etc/bird/bird.conf` 与 `/etc/bird.conf`）以识别 include 路径，并**自动回写**到 config 的 `[paths]`。
- 然后扫描 networkd/NM/wg-quick/Bird peers，将符合 dn42ctl 约定命名的接口导入 DB。
- 若 DB 已有同名 peer：默认提示冲突并跳过，不会静默覆盖。

### 7) Show / Delete

项目还提供巡检与删除：

```bash
dn42ctl show --help
dn42ctl del --help
```

`show` 可选 `--json` 输出结构化结果（便于未来 API 复用）。

## 默认路径（可用参数覆盖）

- config：`/etc/dn42ctl/config.toml`
- SQLite：`/var/lib/dn42ctl/dn42.sqlite3`
- Bird：`/etc/bird/bird.conf`（部分发行版也可能是 `/etc/bird.conf`）
- Bird peers dir：`/etc/bird/peers/`
- Babel：`/etc/bird/babel.conf`
- ROA v6 include：`/etc/bird/roa_dn42_v6.conf`
- systemd-networkd：`/etc/systemd/network/`
- NetworkManager：`/etc/NetworkManager/system-connections/`

## 开发

- 主要代码在 `src/dn42ctl/`
- 规格说明在 `docs/spec.md`
