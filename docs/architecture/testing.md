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
├── conftest.py                          # 共享 fixture
├── test_validators.py                   # 输入校验（纯函数）
├── test_render.py                       # Jinja2 模板渲染
├── test_config.py                       # TOML 配置读写
├── test_db.py                           # SQLite CRUD + 迁移
├── test_db_managed.py                   # managed_nodes CRUD
├── test_db_managed_proposals_reports.py # proposals/reports 存储
├── test_db_managed_revisions.py         # config_revisions 存储
├── test_wg.py                           # WireGuard 子进程
├── test_fs.py                           # 文件权限辅助
├── test_services_core.py               # 服务层公共函数
├── test_services_bgp.py                # BGP peer CRUD
├── test_services_ibgp.py               # iBGP peer CRUD
├── test_services_show.py               # show + 并发 probe
├── test_services_scan.py               # 文件系统扫描
├── test_services_dummy.py              # dummy 接口管理
├── test_services_init_sys.py           # init + genconf
├── test_services_system.py             # system install/uninstall
├── test_services_auto_peer.py          # auto-peer 公共 API 逻辑
├── test_services_crypto_verify.py      # 签名验证
├── test_services_desired_state.py      # desired state 生成
├── test_services_node_admin.py         # 节点管理服务
├── test_services_node_admin_self_toml.py # self 节点 TOML 管理
├── test_services_node_agent.py         # 节点 agent 服务
├── test_services_node_apply.py         # 节点 apply 服务
├── test_services_proposal_decisions.py # proposal accept/reject
├── test_services_proposals_reports.py  # proposal/report 提交
├── test_services_registry.py           # DN42 registry 解析
├── test_services_revisions.py          # revision 管理
├── test_api_admin_nodes.py             # REST API: admin 节点
├── test_api_bgp_peers.py              # REST API: BGP peers
├── test_api_decisions.py              # REST API: proposal decisions
├── test_api_node_routes.py            # REST API: 节点路由
├── test_api_proposals_reports.py      # REST API: proposals/reports
├── test_api_public_auto_peer.py       # REST API: 公共 auto-peer
├── test_api_revisions.py             # REST API: revisions
├── test_cli_node.py                   # CLI: node 命令
├── test_cli_node_decisions.py         # CLI: node decision 命令
├── test_cli_node_push_report.py       # CLI: node push/report
├── test_cli_node_revisions.py         # CLI: node revisions
├── test_cli_node_sync.py             # CLI: node sync
├── test_node_client.py               # node HTTP client
├── test_node_config.py               # node 配置
├── test_node_status.py               # node 状态
└── test_serve_bootstrap.py           # server 启动
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
