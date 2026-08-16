# 默认路径与提权

dn42ctl 默认写入系统目录，因此大部分命令需要 root（例如 `sudo`）权限。
所有路径都可以通过命令行参数或 `config.toml` 覆盖到可写位置，便于非 root 开发与测试。

## 工具自身状态

| 用途 | 默认路径 | 覆盖方式 |
| --- | --- | --- |
| 配置文件 | `/etc/dn42ctl/config.toml` | `--config-path` |
| SQLite 状态库 | `/var/lib/dn42ctl/dn42.sqlite3` | `--db-path` |

> SQLite 中会存放 WireGuard 私钥，代码会尝试 `chmod 0600`，请保持限制性权限。

## dn42 registry（可选）

在 `config.toml` 中通过 `dn42_registry_path = "/var/lib/dn42-registry"` 配置。

- 启用 auto-peer 公共 API 时必需。
- 未配置时 `/api/public/auto-peer/*` 返回 503。

详见 [`auto_peer.md`](auto_peer.md)。

## 生成的配置文件

| 用途 | 默认路径 | 覆盖方式 |
| --- | --- | --- |
| Bird 主配置 | `/etc/bird/bird.conf`（部分发行版为 `/etc/bird.conf`） | `--bird-conf` |
| Bird peers 目录 | `/etc/bird/peers/` | `--bird-peers-dir` |
| Babel 配置 | `/etc/bird/babel.conf` | `--bird-babel-conf` |
| ROA v6 include | `/etc/bird/roa_dn42_v6.conf` | `--bird-roa-v6-conf` |
| systemd-networkd | `/etc/systemd/network/` | `--networkd-dir` |
| NetworkManager（仅 dummy_backend） | `/etc/NetworkManager/system-connections/` | `--nm-system-connections-dir` |

路径覆盖参数在 `init` 时写入 `config.toml` 并保持稳定，详见 [`../commands/init.md`](../commands/init.md)。

## 权限不足时的行为

当权限不足时，程序应给出明确提示：以 root 运行，或通过上述参数覆盖到可写路径。

## 相关文档

- 文件权限与后端细节：[`network_backends.md`](network_backends.md)
- 部署时的目录与 systemd unit：[`deployment.md`](deployment.md)
