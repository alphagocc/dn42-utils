from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("Python 3.11+ is required (tomllib missing)") from exc


@dataclass(frozen=True)
class AppConfig:
    node_id: str
    own_asn: int
    router_id: str
    own_ipv6: str
    ownnet_v6: str
    ownnetset_v6: str
    bird_conf_path: str
    bird_peers_dir: str
    bird_babel_conf_path: str
    bird_roa_v6_conf_path: str
    networkd_dir: str
    nm_system_connections_dir: str


class ConfigError(RuntimeError):
    pass


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Missing/invalid config key: {key}")
    return value


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ConfigError(f"Missing/invalid config key: {key}")
    return value


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config not found: {path}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Failed to read config: {path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid config format: {path}")

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ConfigError("Missing [paths] section")

    return AppConfig(
        node_id=_require_str(raw, "node_id"),
        own_asn=_require_int(raw, "own_asn"),
        router_id=_require_str(raw, "router_id"),
        own_ipv6=_require_str(raw, "own_ipv6"),
        ownnet_v6=_require_str(raw, "ownnet_v6"),
        ownnetset_v6=_require_str(raw, "ownnetset_v6"),
        bird_conf_path=_require_str(paths, "bird_conf"),
        bird_peers_dir=_require_str(paths, "bird_peers_dir"),
        bird_babel_conf_path=_require_str(paths, "bird_babel_conf"),
        bird_roa_v6_conf_path=_require_str(paths, "bird_roa_v6_conf"),
        networkd_dir=_require_str(paths, "networkd_dir"),
        nm_system_connections_dir=_require_str(paths, "nm_system_connections_dir"),
    )


def save_config(path: Path, config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"node_id = {_toml_quote(config.node_id)}",
        f"own_asn = {config.own_asn}",
        f"router_id = {_toml_quote(config.router_id)}",
        f"own_ipv6 = {_toml_quote(config.own_ipv6)}",
        f"ownnet_v6 = {_toml_quote(config.ownnet_v6)}",
        f"ownnetset_v6 = {_toml_quote(config.ownnetset_v6)}",
        "",
        "[paths]",
        f"bird_conf = {_toml_quote(config.bird_conf_path)}",
        f"bird_peers_dir = {_toml_quote(config.bird_peers_dir)}",
        f"bird_babel_conf = {_toml_quote(config.bird_babel_conf_path)}",
        f"bird_roa_v6_conf = {_toml_quote(config.bird_roa_v6_conf_path)}",
        f"networkd_dir = {_toml_quote(config.networkd_dir)}",
        f"nm_system_connections_dir = {_toml_quote(config.nm_system_connections_dir)}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
