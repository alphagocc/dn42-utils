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
├── conftest.py                            # 共享 fixture
├── test_validators.py                     # 输入校验（纯函数）
├── test_render.py                         # Jinja2 模板渲染
├── test_config.py                         # TOML 配置读写
├── test_db.py                             # SQLite CRUD + 迁移
├── test_db_managed.py                     # managed_nodes CRUD
├── test_db_managed_proposals_reports.py   # proposals/reports 存储
├── test_db_managed_revisions.py           # config_revisions 存储
├── test_db_sync_events.py                 # sync_events 发射 / 裁剪 / 游标
├── test_wg.py                             # WireGuard 子进程
├── test_fs.py                             # 文件权限辅助
├── test_services_core.py                  # 服务层公共函数
├── test_services_bgp.py                   # BGP peer CRUD
├── test_services_ibgp.py                  # iBGP peer CRUD
├── test_services_show.py                  # show + 并发 probe
├── test_services_scan.py                  # 文件系统扫描
├── test_services_dummy.py                 # dummy 接口管理
├── test_services_init_sys.py              # init + genconf
├── test_services_system.py                # system install/uninstall
├── test_services_auto_peer.py             # auto-peer 公共 API 逻辑
├── test_services_crypto_verify.py         # 签名验证
├── test_services_desired_state.py         # desired state 生成
├── test_services_node_admin.py            # 节点管理服务
├── test_services_node_admin_self_toml.py  # self 节点 TOML 管理
├── test_services_node_agent.py            # 节点 agent 服务
├── test_services_node_apply.py            # 节点 apply 服务
├── test_services_proposal_decisions.py    # proposal accept/reject
├── test_services_proposals_reports.py     # proposal/report 提交
├── test_services_registry.py              # DN42 registry 解析
├── test_services_revisions.py             # revision 管理
├── test_api_admin_nodes.py                # REST API: admin 节点
├── test_api_bgp_peers.py                  # REST API: BGP peers
├── test_api_decisions.py                  # REST API: proposal decisions
├── test_api_node_routes.py                # REST API: 节点路由
├── test_api_node_ws.py                    # WS: 握手 / 鉴权 / 消息分发
├── test_ws_hub_watcher.py                 # WS: sync_events watcher 端到端推送
├── test_ws_protocol.py                    # WS: 信封编解码
├── test_api_proposals_reports.py          # REST API: proposals/reports
├── test_api_public_auto_peer.py           # REST API: 公共 auto-peer
├── test_api_revisions.py                  # REST API: revisions
├── test_cli_node.py                       # CLI: node 命令
├── test_cli_node_agent.py                 # CLI: node agent 命令
├── test_cli_node_decisions.py             # CLI: node decision 命令
├── test_cli_node_push_report.py           # CLI: node push/report
├── test_cli_node_revisions.py             # CLI: node revisions
├── test_cli_node_sync.py                  # CLI: node sync
├── test_node_client.py                    # node HTTP client
├── test_node_config.py                    # node 配置
├── test_node_status.py                    # node 状态
├── test_node_ws_agent.py                  # 常驻 agent: 重连 / 心跳 / 对账
└── test_serve_bootstrap.py                # server 启动
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

- `subprocess.check_output`：用于 WireGuard、iproute2、Bird 等命令
- `shutil.which`：用于命令探测（nmcli、systemctl、curl）
- `urllib.request.urlopen`：用于 ROA 下载
- `os.chmod` / `os.chown`：文件权限（best-effort 函数）

CI 环境不安装 wireguard-tools 等系统包。

## WebSocket 测试策略

**不引入新的测试依赖。** 没有 `pytest-asyncio`，也没有 anyio 的 pytest 插件。

### Hub 侧：`TestClient.websocket_connect`

`with TestClient(app) as client:` 会在后台 portal 线程里跑 lifespan，于是**真实的** watcher 轮询**真实的** sqlite 文件，而测试线程直接改这个文件。这比 mock 出来的异步框架更接近端到端，且零管道成本。

```python
with TestClient(app) as client:
    with client.websocket_connect(f"/api/v1/nodes/{nid}/ws",
                                  headers={"Authorization": f"Bearer {token}"}) as ws:
        ws.send_json(hello_envelope)
        assert ws.receive_json()["type"] == "hello_ack"
```

- **现有测试不受影响**：它们用裸 `TestClient(app)`（未作为 context manager 使用），lifespan 不会跑，watcher 也就不会起。只有新的 WS 测试用 `with`。

### Spoke 侧：注入缝 + 裸 `asyncio.run()`

`run_agent()` 是 `async def`，`TestClient` 驱动不了。但普通同步测试里 `asyncio.run()` 就够用，**前提是模块留了缝**，即 `run_agent(connect_factory=..., sleep=..., rng=...)`：

- `connect_factory` → 传入假 WS 对象，脚本化收发序列
- `sleep` → 记录延迟并立即返回，第 N 次抛哨兵异常打破无限重连循环
- `rng` → `random.Random(0)`，让 full jitter 的退避值可断言

这三个参数是为可测性而存在的必要设计。**重连循环的测试里没有任何真实 sleep。**

### 否定断言依靠消息顺序

`WebSocketTestSession.receive` **没有超时、会永久阻塞**，所以"断言某次变更没有触发推送"无法写成朴素的"等 X 秒看有没有消息"。

正确做法是靠**顺序**：先改节点 **B**，再改节点 **A**，然后断言 A 的连接收到的**第一条**消息是 A 的推送。watcher 按 `sync_events.id` 顺序处理，所以如果 B 的变更错误地推给了 A，它一定排在前面。无 sleep、无 flake。

### 并发写入

hub 测试里 watcher 线程与测试线程会并发访问同一个 sqlite 文件。项目**不启用 WAL**（原因见 `docs/architecture/database.md`），靠 `PRAGMA busy_timeout=5000` 兜住撞锁。

### respx 的边界

`respx` 只能 mock httpx，对 WebSocket 无用。它继续服务于现有的 HTTP 通道测试（`test_node_client.py` / `test_services_node_agent.py` / `test_cli_node_sync.py`）。

## CI 流水线

GitHub Actions（`.github/workflows/ci.yml`）包含两个并行 job：

- **lint-and-typecheck**：ruff check + ruff format --check + pyright + compileall
- **test**：pytest --cov + 上传覆盖率报告
