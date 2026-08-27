"""Read/write the spoke-side `node.toml` (default: /etc/dn42ctl/node.toml).

The schema:

    server  = "https://center.example"   # or "http://[::1]:4242" for self node
    node_id = "<uuid>"
    token   = "<plaintext>"

    [apply]
    bird_conf_path = "..."         # all optional; override default paths returned
    peers_dir      = "..."         # by central server's desired-state response.
    babel_conf_path = "..."
    networkd_dir = "..."
    nm_dir = "..."
    config_path = "..."            # 本机 /etc/dn42ctl/config.toml,仅在中心下发
                                   # own_ipv6/router_id 时才会被读写
    reload = "auto"                # "auto" | "never" —— apply 后是否 reload
                                   # networkd/bird。不是路径,单独解析到
                                   # NodeConfig.reload_policy。

    [cache]
    db_path = "/var/lib/dn42ctl/node-cache.sqlite3"

    [agent]                            # all optional, tuning for `dn42ctl node agent`
    reconnect_initial_seconds  = 1.0
    reconnect_max_seconds      = 60.0
    auth_retry_seconds         = 300.0
    reconcile_interval_seconds = 900.0
    heartbeat_interval_seconds = 60.0
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from dn42ctl.constants import FILE_MODE_PRIVATE, RELOAD_POLICY_AUTO, VALID_RELOAD_POLICIES
from dn42ctl.fs import chmod_best_effort
from dn42ctl.paths import NODE_CACHE_DB_PATH


class NodeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentOptions:
    """Tuning for the resident agent. Defaults are what production should use.

    There is deliberately no `enabled` flag: the kill switch is
    `systemctl stop dn42ctl-node-agent` plus a manual `dn42ctl node once`. A
    config toggle would only invite a half-migrated fleet where the unit is
    running but silently doing nothing.
    """

    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    # Auth-fatal closes get a long fixed wait rather than the exponential ramp:
    # a stale token retrying every second is a reconnect storm against the hub.
    auth_retry_seconds: float = 300.0
    reconcile_interval_seconds: float = 900.0
    heartbeat_interval_seconds: float = 60.0


DEFAULT_AGENT_OPTIONS = AgentOptions()


@dataclass(frozen=True)
class NodeConfig:
    server: str
    node_id: str
    token: str
    apply_overrides: dict[str, str] = field(default_factory=dict)
    cache_db_path: Path = field(default_factory=lambda: NODE_CACHE_DB_PATH)
    agent: AgentOptions = field(default_factory=AgentOptions)
    # apply 之后是否 reload networkd/bird。reload 是**节点本地决策**,hub 侧不设开关。
    reload_policy: str = RELOAD_POLICY_AUTO


def _require_str(data: dict[str, Any], key: str, *, file: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise NodeConfigError(f"{file}: 缺失或类型错误的字段 '{key}'")
    return value


def _parse_agent_block(block: Any, *, file: Path) -> AgentOptions:
    if not isinstance(block, dict):
        raise NodeConfigError(f"{file}: [agent] 段格式错误")
    known = {f: getattr(DEFAULT_AGENT_OPTIONS, f) for f in asdict(DEFAULT_AGENT_OPTIONS)}
    values: dict[str, float] = {}
    for key, raw_value in block.items():
        if key not in known:
            raise NodeConfigError(f"{file}: [agent] 未知字段 '{key}'")
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise NodeConfigError(f"{file}: [agent].{key} 必须是数字")
        if raw_value <= 0:
            raise NodeConfigError(f"{file}: [agent].{key} 必须大于 0")
        values[key] = float(raw_value)
    return AgentOptions(**{**known, **values})


def load_node_config(path: Path) -> NodeConfig:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as exc:
        raise NodeConfigError(f"node.toml 不存在: {path}") from exc
    except Exception as exc:  # noqa: BLE001
        raise NodeConfigError(f"读取 node.toml 失败: {path}") from exc

    if not isinstance(raw, dict):
        raise NodeConfigError(f"node.toml 格式错误: {path}")

    server = _require_str(raw, "server", file=path)
    node_id = _require_str(raw, "node_id", file=path)
    token = _require_str(raw, "token", file=path)

    apply_overrides: dict[str, str] = {}
    reload_policy = RELOAD_POLICY_AUTO
    apply_block = raw.get("apply")
    if apply_block is not None:
        if not isinstance(apply_block, dict):
            raise NodeConfigError(f"{path}: [apply] 段格式错误")
        for k, v in apply_block.items():
            if not isinstance(v, str):
                raise NodeConfigError(f"{path}: [apply].{k} 必须是字符串")
            if k == "reload":
                # reload 不是路径覆盖,不能混进 apply_overrides(那是 _resolve_paths
                # 消费的 dict[str, str] 路径表)。
                if v not in VALID_RELOAD_POLICIES:
                    raise NodeConfigError(f"{path}: [apply].reload 必须是 {' 或 '.join(VALID_RELOAD_POLICIES)}")
                reload_policy = v
                continue
            apply_overrides[k] = v

    cache_db_path = NODE_CACHE_DB_PATH
    cache_block = raw.get("cache")
    if cache_block is not None:
        if not isinstance(cache_block, dict):
            raise NodeConfigError(f"{path}: [cache] 段格式错误")
        db_str = cache_block.get("db_path")
        if db_str is not None:
            if not isinstance(db_str, str):
                raise NodeConfigError(f"{path}: [cache].db_path 必须是字符串")
            cache_db_path = Path(db_str)

    agent = DEFAULT_AGENT_OPTIONS
    agent_block = raw.get("agent")
    if agent_block is not None:
        agent = _parse_agent_block(agent_block, file=path)

    return NodeConfig(
        server=server,
        node_id=node_id,
        token=token,
        apply_overrides=apply_overrides,
        cache_db_path=cache_db_path,
        agent=agent,
        reload_policy=reload_policy,
    )


def save_node_config(path: Path, config: NodeConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "server": config.server,
        "node_id": config.node_id,
        "token": config.token,
    }
    if config.apply_overrides:
        data["apply"] = dict(config.apply_overrides)
    if config.reload_policy != RELOAD_POLICY_AUTO:
        data.setdefault("apply", {})["reload"] = config.reload_policy
    if config.cache_db_path != NODE_CACHE_DB_PATH:
        data["cache"] = {"db_path": str(config.cache_db_path)}
    if config.agent != DEFAULT_AGENT_OPTIONS:
        # Only write the block when it differs, so `node init` and self
        # registration keep producing the same minimal file as before.
        data["agent"] = asdict(config.agent)
    with path.open("wb") as f:
        tomli_w.dump(data, f)
    chmod_best_effort(path, FILE_MODE_PRIVATE)
