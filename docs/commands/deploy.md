# deploy

部署 Web UI 或 daemon 到系统目录。

## deploy web

构建 Web UI 并复制到指定目录。

```bash
dn42ctl deploy web <dest>
dn42ctl deploy web --api-base https://api.dn42.example.com <dest>
dn42ctl deploy web --skip-build <dest>
```

| 参数 | 说明 |
|------|------|
| `dest` | 部署目标目录 (如 `/var/www/dn42ctl`) |
| `--api-base` | 设置 `VITE_API_BASE` 环境变量，指定前端 API 基址 (仅构建时生效) |
| `--skip-build` | 跳过构建，直接复制已有 `dist/` |

行为：构建 → 复制 `dist/{admin,peer,assets}` → `restorecon`（如有）。

```bash
sudo dn42ctl deploy web --api-base https://api.dn42.example.com /var/www/dn42ctl
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

venv 放在 `--tool-dir` 指定的目录，避开 `~/.local/share/uv/tools/`，防止 systemd `ProtectHome=true` 导致启动失败。

```bash
sudo dn42ctl deploy daemon
```

## 实现分层

两个子命令的实现都在 [`src/dn42ctl/services/deploy.py`](../../src/dn42ctl/services/deploy.py)，
CLI 侧只负责参数声明与结果渲染。

`deploy_web(*, dest, skip_build=False, api_base="")` 与
`deploy_daemon(*, dest, tool_dir)` 均返回 `DeployResult`：

| 字段 | 内容 |
| --- | --- |
| `action` | `"web"` 或 `"daemon"` |
| `changed_files` | 实际写入或替换的目标 |
| `notes` | 值得打印但并非文件变更的信息，例如注入的 commit |
| `warnings` | 未中断执行的问题，例如 `restorecon` 调用失败 |

失败一律抛 `Dn42CtlError`，由 CLI 的 `@cli_errors` 装饰器转成退出码 1，见
[`../architecture/cli_errors.md`](../architecture/cli_errors.md)。

`pnpm` 与 `uv` 的子进程输出继承到终端而非被捕获：这两个命令都是分钟级的，进度需要实时可见。
因此服务层不返回它们的输出。

`restorecon` 找不到时静默跳过，非 SELinux 系统属于常态；调用存在却失败则记入 `warnings`。
标签不正确的静态文件会让 nginx 读不到，而现象只表现为 403，静默丢弃这个失败会让排查变得很费时。

`deploy` 需要在源码检出中运行：包根位置由 `services/deploy.py` 自身的位置推算，
`deploy web` 还要读取仓库里的 `web/package.json`。
