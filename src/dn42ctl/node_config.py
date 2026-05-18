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

    [cache]
    db_path = "/var/lib/dn42ctl/node-cache.sqlite3"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from dn42ctl.constants import FILE_MODE_PRIVATE
from dn42ctl.fs import chmod_best_effort
from dn42ctl.paths import NODE_CACHE_DB_PATH


class NodeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeConfig:
    server: str
    node_id: str
    token: str
    apply_overrides: dict[str, str] = field(default_factory=dict)
    cache_db_path: Path = field(default_factory=lambda: NODE_CACHE_DB_PATH)


def _require_str(data: dict[str, Any], key: str, *, file: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise NodeConfigError(f"{file}: 缺失或类型错误的字段 '{key}'")
    return value


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
    apply_block = raw.get("apply")
    if apply_block is not None:
        if not isinstance(apply_block, dict):
            raise NodeConfigError(f"{path}: [apply] 段格式错误")
        for k, v in apply_block.items():
            if not isinstance(v, str):
                raise NodeConfigError(f"{path}: [apply].{k} 必须是字符串")
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

    return NodeConfig(
        server=server,
        node_id=node_id,
        token=token,
        apply_overrides=apply_overrides,
        cache_db_path=cache_db_path,
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
    if config.cache_db_path != NODE_CACHE_DB_PATH:
        data["cache"] = {"db_path": str(config.cache_db_path)}
    with path.open("wb") as f:
        tomli_w.dump(data, f)
    chmod_best_effort(path, FILE_MODE_PRIVATE)
