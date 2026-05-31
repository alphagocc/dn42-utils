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

通过 `uv tool install` 安装独立的 dn42ctl 到系统路径，供 systemd service 调用。

```bash
sudo dn42ctl deploy daemon
sudo dn42ctl deploy daemon --dest /usr/local/bin --tool-dir /opt/dn42ctl
```

| 参数 | 说明 |
|------|------|
| `--dest` | 可执行文件安装目录 (默认 `/usr/local/bin`) |
| `--tool-dir` | uv tool venv 目录 (默认 `/opt/dn42ctl`) |

venv 放在 `--tool-dir` 而非 `~/.local/share/uv/tools/`，避免 systemd `ProtectHome=true` 导致无法启动。

```bash
sudo dn42ctl deploy daemon
```
