# 测试基础设施

## 框架与工具

| 工具 | 用途 |
|------|------|
| pytest | 测试框架 |
| pytest-cov | 覆盖率报告 |
| ruff | Lint + 格式化 |
| pyright | 静态类型检查 |

开发依赖通过 `[dependency-groups] dev` 管理（PEP 735），使用 `uv sync --group dev` 安装。

## 运行测试

```bash
# 运行全部测试
uv run pytest -v

# 带覆盖率
uv run pytest --cov=dn42ctl --cov-report=term-missing

# 运行单个文件
uv run pytest tests/test_validators.py -v

# Lint
uv run ruff check src/ tests/

# 类型检查
uv run pyright src/
```

## 目录结构

```
tests/
├── conftest.py              # 共享 fixture
├── test_validators.py       # 输入校验（纯函数）
├── test_render.py           # Jinja2 模板渲染
├── test_config.py           # TOML 配置读写
├── test_db.py               # SQLite CRUD + 迁移
├── test_wg.py               # WireGuard 子进程
├── test_fs.py               # 文件权限辅助
├── test_services_core.py    # 服务层公共函数
├── test_services_bgp.py     # BGP peer CRUD
├── test_services_ibgp.py    # iBGP peer CRUD
├── test_services_show.py    # show + 并发 probe
├── test_services_scan.py    # 文件系统扫描
├── test_services_dummy.py   # dummy 接口管理
├── test_services_init_sys.py # init + genconf
└── test_api.py              # FastAPI REST API
```

## Fixture 设计

### `sample_config(tmp_path)`

返回一个 `AppConfig`，所有路径指向 `tmp_path` 子目录，测试间完全隔离。

### `mem_db()` / `mem_db_with_node()`

使用 SQLite `:memory:` 数据库，已运行全部 migration。`mem_db_with_node` 额外预插入 `"test-node"` 节点。

### `mock_wg_keypair()`

Patch `generate_wg_keypair()` 返回固定密钥对，避免依赖系统 `wg` 命令。

## Mock 策略

生产代码调用多个系统命令（`wg`、`ip`、`nmcli`、`birdc`、`systemctl`），测试中**全部 mock**：

- `subprocess.check_output` — 用于 WireGuard、iproute2、Bird 等命令
- `shutil.which` — 用于命令探测（nmcli、systemctl、curl）
- `urllib.request.urlopen` — 用于 ROA 下载
- `os.chmod` / `os.chown` — 文件权限（best-effort 函数）

CI 环境不安装 wireguard-tools 等系统包。

## CI 流水线

GitHub Actions（`.github/workflows/ci.yml`）包含两个并行 job：

- **lint-and-typecheck**：ruff check + ruff format --check + pyright + compileall
- **test**：pytest --cov + 上传覆盖率报告
