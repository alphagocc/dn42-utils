# plan-2.md — Anti-DRY 重构计划 v2（精确到文件/行号）

> 基于对 `src/` 下所有源文件的逐行审查，列出 18 个 Anti-DRY 违规及其具体修复方案。
> 行为不变优先：所有重构保持 CLI 输出、退出码、生成文件内容不变。

---

## Phase 1: 基础设施层（零业务依赖，零行为改动）

### 1.1 新增 `src/dn42ctl/constants.py`

集中管理跨模块散落的魔法值：

```python
MAX_PORT = 65535
FILE_MODE_PRIVATE = 0o600
BABEL_DEFAULT_RXCOST = 120
WG_PORT_RANGE = (20000, 65535)
NET_BACKEND_NETWORKD = "networkd"
NET_BACKEND_NM = "nm"
IFNAME_PREFIX_BGP = "dn42_"
IFNAME_PREFIX_IBGP = "wg_"
LIVE_CMD_TIMEOUT = 2
```

**替换点**：
- `cli.py:103` 的 `65535` → `MAX_PORT`
- `bgp.py:62,65,170`、`ibgp.py:64,75,226` 的 `65535` → `MAX_PORT`
- `config.py:106`、`db.py:73`、`core.py:255,282` 的 `0o600` → `FILE_MODE_PRIVATE`
- `scan.py:625,664` 的 `120` → `BABEL_DEFAULT_RXCOST`
- `show.py` 的 `_LIVE_CMD_TIMEOUT = 2` → `LIVE_CMD_TIMEOUT`
- `core.py:207-208` 的 `20000, 65535` → `WG_PORT_RANGE`
- `scan.py:134` 的 `"dn42_"` / `"wg_"` → 常量引用

> **注意**：`migrations.py` 中 SQL `DEFAULT 120` 是 DDL 历史记录，不替换。

### 1.2 提取 `chmod_best_effort` 到独立位置

当前 `config.py:105-108`、`db.py:72-75`、`core.py:163-167` 各自实现了相同的 try/except OSError pass。

**方案**：在 `core.py` 已有的 `_chmod_if_possible` 改名为 `chmod_best_effort` 并提升到 `src/dn42ctl/fs.py`（新文件），`config.py`、`db.py`、`core.py` 统一导入复用。

### 1.3 新增 `src/dn42ctl/validators.py`

纯函数，不依赖 services/cli：

```python
def validate_listen_port(value: int, *, allow_zero: bool = False) -> int: ...
def validate_rxcost(value: int) -> int: ...
```

**替换点**：
- `bgp.py:62-65`, `bgp.py:169-170`, `ibgp.py:63-64` → `validate_listen_port()`
- `ibgp.py:74-75`, `ibgp.py:225-226`, `cli.py:101-104` → `validate_rxcost()`

---

## Phase 2: services 层去重

### 2.1 Babel 配置重生成统一（消灭 4 处复制粘贴）

**当前重复点**：
- `ibgp.py:140-150`（create）
- `ibgp.py:197-206`（delete）
- `ibgp.py:246-256`（modify_rxcost）
- `init_sys.py:110-121`（genconf）

四处完全相同的逻辑：
```python
interfaces = [(str(r["ifname"]), int(r["babel_rxcost"])) for r in db.list_ibgp_peers(node_id)]
babel_text = render_babel_conf(interfaces=interfaces)
babel_path = Path(config.bird_babel_conf_path)
write_text(babel_path, babel_text)
```

**方案**：在 `services/core.py` 新增：
```python
def regenerate_babel_conf(*, config: AppConfig, db: Database, node_id: str) -> Path:
```
四个调用点替换为一行调用。

### 2.2 WG keypair 解析/生成统一

**当前重复点**：
- `bgp.py:67-76` 与 `ibgp.py:77-86`：完全相同的 10 行。

**方案**：在 `services/core.py` 新增：
```python
def resolve_wg_keypair(
    wg_private_key: str | None, wg_public_key: str | None
) -> tuple[str, str]:
```

### 2.3 WG pubkey subprocess 统一

**当前重复点**：
- `wg.py:16-17`（`generate_wg_keypair` 内部 `wg pubkey` 调用）
- `scan.py:319-331`（`_wg_pubkey_from_private` 独立实现）

**方案**：在 `wg.py` 新增 `pubkey_from_private(key: str) -> str`。
- `generate_wg_keypair` 内部改为调用 `pubkey_from_private`。
- `scan.py` 删除 `_wg_pubkey_from_private`，改为 `from dn42ctl.wg import pubkey_from_private`。

### 2.4 AllowedIPs JSON 解析统一

**当前重复点**：
- `show.py:86-100`（`_parse_allowed_ips_json`）
- `bgp.py:188-189`（modify 中裸 `json.loads` + fallback）

**方案**：将 `_parse_allowed_ips_json` 提升到 `services/core.py` 并导出。`bgp.py` modify 替换为调用该函数。

### 2.5 Bird BGP peer conf 渲染+写入统一

**当前重复点**：
- `bgp.py:103-111`（create）与 `bgp.py:206-214`（modify）

**方案**：提取到 `services/core.py` 或 `bgp.py` 内部：
```python
def _write_bird_bgp_peer(config, ifname, peer_lla, peer_asn, generated): ...
```

### 2.6 `open_db` + `ensure_node` + `DatabaseError -> Dn42CtlError` 包装统一

**当前重复点**：`bgp.py:42-46`, `bgp.py:151-155`, `ibgp.py:43-48`, `init_sys.py:65-69`, `init_sys.py:87-91`

**方案**：在 `services/core.py` 新增：
```python
def open_db_and_ensure_node(db_path: Path, node_id: str) -> Database: ...
```

---

## Phase 3: CLI 层去重

### 3.1 `require_config` 错误处理统一

**当前重复**：8+ 处完全相同的 try/except/typer.Exit 块。

**方案**：在 `cli.py` 新增：
```python
def _require_config_or_exit(appctx: AppContext) -> AppConfig:
    try:
        return appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc
```

所有命令函数替换为 `config = _require_config_or_exit(appctx)`。

### 3.2 `open_db` 错误处理统一

**当前重复**：`cli.py:600-609` 与 `cli.py:797-806`。

**方案**：新增 `_open_db_or_exit(appctx) -> Database`。

### 3.3 BGP/iBGP 交互式输入流程统一

**当前重复**：`cli.py:509-543` 与 `cli.py:713-749`。

**方案**：提取：
```python
@dataclass
class _PreparedPeerInfo:
    wg_private_key: str | None
    wg_public_key: str | None
    local_lla: str | None
    peer_public_key: str
    endpoint: str
    peer_lla: str

def _prepare_peer_info(
    peer_public_key: str | None,
    endpoint: str | None,
    peer_lla: str | None,
) -> _PreparedPeerInfo: ...
```

### 3.4 show 打印逻辑统一

**当前重复**：`cmd_show_wg` 的打印 ≡ `cmd_show_all` 的 wg 段（逐行相同），bgp/ibgp 同理。

**方案**：提取三个格式化函数：
```python
def _print_wg_tunnels(tunnels: list[WgTunnelView]) -> None: ...
def _print_bgp_peers(peers: list[BgpPeerView]) -> None: ...
def _print_ibgp_peers(peers: list[IbgpPeerView]) -> None: ...
```

各 show 命令和 show all 共用。

### 3.5 `cmd_scan` 的 discovery replace 循环化

**当前重复**：4 段结构完全相同的 if 块 (`cli.py:390-418`)。

**方案**：
```python
_DISCOVERY_FIELDS = [
    ("bird_conf_path", "bird_conf_path"),
    ("bird_peers_dir", "bird_peers_dir"),
    ("bird_babel_conf_path", "bird_babel_conf_path"),
    ("bird_roa_v6_conf_path", "bird_roa_v6_conf_path"),
]
for disc_attr, cfg_attr in _DISCOVERY_FIELDS:
    disc_val = getattr(discovery, disc_attr)
    if disc_val is not None and str(disc_val) != str(getattr(config, cfg_attr)):
        updated_config = replace(updated_config, **{cfg_attr: str(disc_val)})
        updated = True
```

### 3.6 genconf 覆盖确认统一

**当前重复**：`cmd_init:246-259` 与 `cmd_genconf:330-340`。

**方案**：提取 `_confirm_overwrite_if_exists(path: Path) -> bool`。

---

## Phase 4: 数据结构层（可选，低优先级）

### 4.1 `BgpPeerRecord` / `IbgpPeerRecord` 公共字段提取

**当前**：12 个字段中 10 个完全相同。

**方案**：
```python
@dataclass(frozen=True)
class _PeerRecordBase:
    node_id: str
    ifname: str
    wg_private_key: str
    wg_public_key: str
    peer_public_key: str | None
    endpoint: str | None
    local_lla: str
    peer_lla: str | None
    listen_port: int
    allowed_ips: list[str]
    net_backend: str

@dataclass(frozen=True)
class BgpPeerRecord(_PeerRecordBase):
    peer_asn: int

@dataclass(frozen=True)
class IbgpPeerRecord(_PeerRecordBase):
    name: str
    babel_rxcost: int
```

### 4.2 `BgpPeerView` / `IbgpPeerView` 公共字段提取

同上策略。

### 4.3 `Database.delete_bgp_peer` / `delete_ibgp_peer` 泛化

**当前重复**：相同的 get→check→delete→commit 模式。

**方案**：内部抽出 `_delete_peer(table, get_fn, ...)` 泛化方法。

---

## 执行顺序建议

| 优先级 | 编号 | 内容 | 风险 |
|--------|------|------|------|
| P0 | 1.1 | 常量提取 | 极低（纯替换字面值） |
| P0 | 1.2 | chmod 统一 | 极低 |
| P0 | 1.3 | validators 提取 | 低 |
| P1 | 2.1 | babel 重生成统一 | 低（需验证输出一致） |
| P1 | 2.2 | WG keypair 统一 | 低 |
| P1 | 2.3 | WG pubkey 统一 | 低 |
| P1 | 2.4 | AllowedIPs 统一 | 低 |
| P1 | 2.5 | Bird BGP conf 写入统一 | 低 |
| P1 | 2.6 | open_db + ensure_node 统一 | 低 |
| P2 | 3.1 | CLI require_config 统一 | 低（需保持退出码） |
| P2 | 3.2 | CLI open_db 统一 | 低 |
| P2 | 3.3 | CLI 交互输入统一 | 中（涉及用户交互） |
| P2 | 3.4 | CLI show 打印统一 | 低（需保持输出格式） |
| P2 | 3.5 | scan discovery 循环化 | 低 |
| P2 | 3.6 | genconf 覆盖确认统一 | 低 |
| P3 | 4.1-4.3 | 数据结构基类提取 | 中（改动接口面较大） |

## 执行决策记录（2026-04-22 确认）

- **Phase 4**：包含（4.1-4.3 全部执行）
- **Commit 粒度**：每个 Phase 一个 commit（共 4 个 commit）
- **排除项**：无，全部按计划执行
- **常量命名**：按 plan 原样（MAX_PORT、WG_PORT_RANGE tuple 等）

## 验证清单（每个 Phase 完成后执行）

1. `python -m compileall -q src` — 编译检查
2. `dn42ctl --help` — CLI 入口正常
3. 对比重构前后 `show all` / `genconf` / `bgp peer` / `ibgp peer` 的输出
4. 确认所有错误消息和退出码与重构前一致
