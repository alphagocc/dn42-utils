# deploy

部署 Web UI 或 daemon 到系统目录。

## deploy web

构建 Web UI 并复制到指定目录。

```bash
dn42ctl deploy web <dest>
dn42ctl deploy web --skip-build <dest>
```

| 参数 | 说明 |
|------|------|
| `dest` | 部署目标目录 (如 `/var/www/dn42ctl`) |
| `--skip-build` | 跳过构建，直接复制已有 `dist/` |

行为：构建 → 复制 `dist/{admin,peer,assets}` → `restorecon`（如有）。

```bash
sudo dn42ctl deploy web /var/www/dn42ctl
```

## deploy daemon

安装 dn42ctl 可执行入口到系统路径，供 systemd service 调用。

```bash
dn42ctl deploy daemon
dn42ctl deploy daemon --dest /usr/local/bin/dn42ctl
```

| 参数 | 说明 |
|------|------|
| `--dest` | 安装路径 (默认 `/usr/local/bin/dn42ctl`) |

行为：生成 wrapper 脚本（通过 `uv run --project` 调用）→ 设置权限 0755 → `restorecon`（如有）→ 预热 `uv sync`。

```bash
sudo dn42ctl deploy daemon
```
