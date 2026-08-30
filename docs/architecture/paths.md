# 默认路径与提权

dn42ctl 默认写入系统目录，因此大部分命令需要 root（例如 `sudo`）权限。所有路径都可以通过命令行参数或 `config.toml` 覆盖到可写位置，便于非 root 开发与测试。

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
| Bird 主配置 | `/etc/bird.conf` | `--bird-conf` |
| Bird peers 目录 | `/etc/bird/peers/` | `--bird-peers-dir` |
| Babel 配置 | `/etc/bird/babel.conf` | `--bird-babel-conf` |
| ROA v6 include | `/etc/bird/roa_dn42_v6.conf` | `--bird-roa-v6-conf` |
| Bird 自定义配置 | `/etc/bird/extra.conf` | `--bird-extra-conf` |
| systemd-networkd | `/etc/systemd/network/` | `--networkd-dir` |
| NetworkManager（仅 dummy_backend） | `/etc/NetworkManager/system-connections/` | `--nm-system-connections-dir` |

路径覆盖参数在 `init` 时写入 `config.toml` 并保持稳定，详见 [`../commands/init.md`](../commands/init.md)。

`extra.conf` 是用户手工维护的 Bird 配置，工具只保证它被 `include` 且文件存在，内容永不改写，详见 [`bird_extra_conf.md`](bird_extra_conf.md)。

Bird 相关文件写为 `0644`：bird 以 `-u bird -g bird` 降权运行，`birdc configure` 触发的重读发生在降权之后，`0600 root` 会让重读失败。`extra.conf` 由使用者维护，需要收紧时自行 `chmod`，工具只在该文件缺失时写入占位内容，此后不再改动它。

## 写入位置的解析

文件布局是每台机器自己的属性。`dn42ctl node apply` 与 CLI 的 `genconf` 读取同一处，即本机 `config.toml` 的 `[paths]` 段，因此两条渲染出口不会分叉。该文件缺失或无法解析时（只跑过 `node init` 的纯 spoke）落到 `src/dn42ctl/paths.py` 的内置默认值。

`config.toml` 自身的位置由全局参数 `--config-path` 决定，默认 `/etc/dn42ctl/config.toml`，环境变量 `DN42CTL_CONFIG` 同样生效。常驻 agent 沿用同一参数。

## Bird 主配置的位置

bird 在 Fedora 上读的是 `/etc/bird.conf`：`bird.service` 的 `ExecStart` 不带 `-c`，用的是编译内置的位置。dn42ctl 的默认值与之一致，其余文件仍在 `/etc/bird/` 目录下（peers 目录、`babel.conf`、`extra.conf`、ROA），由主配置 `include` 引用，因此不需要给 `bird.service` 加 drop-in。已有的 `config.toml` 不受默认值变动影响，`init` 优先沿用文件里已经写好的值。

该文件由 `genconf` 创建，agent 只负责后续更新：unit 的 `ReadWritePaths` 里该项写作 `-/etc/bird.conf`，前缀 `-` 的含义是"文件不存在就跳过这一项"，跳过之后 `/etc` 对 agent 就是只读的。

更新它的时候没有原子替换。systemd 把它作为单个文件挂进 agent 的命名空间，`/etc` 目录仍然只读（建不了临时文件），挂载点也不能被改名覆盖，于是只能原地覆写（`services/node_apply.py` 的 `_write_in_place`）。agent 恰好在写这个文件时被杀掉会留下半截内容：运行中的 bird 不受影响，但下次开机 `ExecStartPre=/usr/sbin/bird -p` 会校验失败而拒绝启动，`sudo dn42ctl genconf && sudo systemctl start bird` 恢复。

启用 SELinux 的机器上该文件的上下文是 `dn42ctl_bird_conf_t`，由 `selinux/dn42ctl.fc` 声明，详见 [`selinux.md`](selinux.md)。

## 权限不足时的行为

当权限不足时，程序应给出明确提示：以 root 运行，或通过上述参数覆盖到可写路径。

## 相关文档

- 文件权限与后端细节：[`network_backends.md`](network_backends.md)
- 部署时的目录与 systemd unit：[`deployment.md`](deployment.md)
