from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dn42ctl.config import AppConfig
from dn42ctl.constants import FILE_MODE_NETDEV, FILE_MODE_PRIVATE, WG_PORT_RANGE
from dn42ctl.db import Database, DatabaseError
from dn42ctl.fs import chmod_best_effort, chown_best_effort
from dn42ctl.render import (
    nm_uuid_for,
    render_babel_conf,
    render_bird_bgp_peer_conf,
    render_networkd_netdev,
    render_networkd_network,
    render_nmconnection_wireguard,
)
from dn42ctl.wg import WireGuardError, generate_wg_keypair

DEFAULT_ALLOWED_IPS = ["fe80::/64", "fd00::/8"]
IBGP_ALLOWED_IPS = ["fe80::/64", "fd00::/8", "ff02::/16"]


class Dn42CtlError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandOutput:
    cmd: list[str]
    ok: bool
    output: str | None
    error: str | None


@dataclass(frozen=True)
class FileStatus:
    path: str
    exists: bool


@dataclass(frozen=True)
class _PeerViewBase:
    ifname: str
    peer_public_key: str | None
    endpoint: str | None
    peer_lla: str | None
    local_lla: str
    listen_port: int
    allowed_ips: list[str]
    net_backend: str
    wg_public_key: str
    files: list[FileStatus]
    live_wg: CommandOutput | None
    live_bird: CommandOutput | None


@dataclass(frozen=True)
class BgpPeerView(_PeerViewBase):
    peer_asn: int


@dataclass(frozen=True)
class IbgpPeerView(_PeerViewBase):
    name: str
    babel_rxcost: int
    babel_type: str
    peer_ip: str | None
    has_wg: bool


@dataclass(frozen=True)
class WgTunnelView:
    kind: str  # "bgp" | "ibgp"
    peer_asn: int | None
    name: str | None
    ifname: str
    peer_public_key: str | None
    endpoint: str | None
    allowed_ips: list[str]
    listen_port: int
    local_lla: str
    peer_lla: str | None
    net_backend: str
    wg_public_key: str
    files: list[FileStatus]
    live_wg: CommandOutput | None


@dataclass(frozen=True)
class DeleteResult:
    kind: str  # "bgp" | "ibgp"
    peer_asn: int | None
    name: str | None
    deleted_files: list[str]
    missing_files: list[str]
    regenerated_files: list[str]


@dataclass(frozen=True)
class ScanImported:
    kind: str  # "bgp" | "ibgp"
    key: str  # "AS4242..." | "name"
    ifname: str
    net_backend: str


@dataclass(frozen=True)
class ScanResult:
    inserted: list[ScanImported]
    conflicts: list[ScanImported]
    skipped: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class BirdPathsDiscovery:
    bird_conf_path: Path | None
    bird_peers_dir: Path | None
    bird_babel_conf_path: Path | None
    bird_roa_v6_conf_path: Path | None
    warnings: list[str]


def permission_hint() -> str:
    return (
        "请确认当前用户对该路径有写权限；"
        "若使用默认系统路径，通常需要以 root 运行（sudo）。"
        "也可以通过 --config-path/--db-path 或 init 的路径参数覆盖输出目录。"
    )


from dn42ctl.services.dummy import DummyResult


@dataclass(frozen=True)
class InitConfigResult:
    config: AppConfig
    config_path: Path
    db_path: Path
    dummy: DummyResult | None


@dataclass(frozen=True)
class GenConfResult:
    config: AppConfig
    db_path: Path
    bird_conf_path: Path
    bird_babel_conf_path: Path
    bird_roa_v6_conf_path: Path
    systemd_roa_timer_enabled: bool
    dummy: DummyResult | None
    warnings: list[str]


@dataclass(frozen=True)
class PeerResult:
    ifname: str
    listen_port: int
    wg_public_key: str
    local_lla: str
    generated_files: list[Path]


def write_text(path: Path, content: str, *, mode: int | None = None, owner: tuple[int, str] | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法写入 {path}。{permission_hint()}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"写入失败: {path} ({exc})") from exc
    if mode is not None:
        chmod_best_effort(path, mode)
    if owner is not None:
        chown_best_effort(path, owner[0], owner[1])


def ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法创建目录 {path}。{permission_hint()}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"创建目录失败: {path} ({exc})") from exc


def open_db(db_path: Path) -> Database:
    try:
        return Database.open(db_path)
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法创建/写入数据库 {db_path}。{permission_hint()}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"打开数据库失败: {db_path} ({exc})") from exc
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc


def pick_unused_port(used: set[int]) -> int:
    # Keep away from the well-known/default WG port; prefer high ports.
    candidate = random.randint(*WG_PORT_RANGE)
    attempts = 0
    while candidate in used:
        candidate = random.randint(*WG_PORT_RANGE)
        attempts += 1
        if attempts > 2000:
            raise Dn42CtlError("无法自动选择未占用端口，请手动指定")
    return candidate


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise Dn42CtlError("名称不能为空")
    return cleaned.lower()


def write_net_backend_files(
    *,
    config: AppConfig,
    node_id: str,
    backend: str,
    ifname: str,
    private_key: str,
    listen_port: int,
    peer_public_key: str,
    endpoint: str,
    allowed_ips: list[str],
    local_lla: str,
    peer_lla: str,
    generated: list[Path],
) -> None:
    """Write networkd or NetworkManager wireguard config files."""
    if backend == "networkd":
        netdev_path = Path(config.networkd_dir) / f"{ifname}.netdev"
        network_path = Path(config.networkd_dir) / f"{ifname}.network"
        write_text(
            netdev_path,
            render_networkd_netdev(
                ifname=ifname,
                private_key=private_key,
                listen_port=listen_port,
                peer_public_key=peer_public_key,
                endpoint=endpoint,
                allowed_ips=allowed_ips,
            ),
            mode=FILE_MODE_NETDEV,
            owner=(0, "systemd-network"),
        )
        write_text(
            network_path,
            render_networkd_network(
                ifname=ifname,
                local_lla=local_lla,
                peer_lla=peer_lla,
            ),
        )
        generated.extend([netdev_path, network_path])
    elif backend == "nm":
        nm_path = Path(config.nm_system_connections_dir) / f"{ifname}.nmconnection"
        write_text(
            nm_path,
            render_nmconnection_wireguard(
                conn_id=ifname,
                ifname=ifname,
                conn_uuid=nm_uuid_for(node_id=node_id, ifname=ifname),
                private_key=private_key,
                listen_port=listen_port,
                peer_public_key=peer_public_key,
                endpoint=endpoint,
                allowed_ips=allowed_ips,
                local_lla=local_lla,
                peer_lla=peer_lla,
            ),
            mode=FILE_MODE_PRIVATE,
        )
        generated.append(nm_path)


def normalize_net_backend(net_backend: str) -> str:
    from dn42ctl.validators import ValidationError, validate_net_backend

    try:
        return validate_net_backend(net_backend)
    except ValidationError as exc:
        raise Dn42CtlError(str(exc)) from exc


def peer_files_for_backend(
    *,
    config: AppConfig,
    ifname: str,
    net_backend: str,
    kind: str,
    ibgp_name: str | None = None,
) -> list[Path]:
    files: list[Path] = []
    bird_peers_dir = Path(config.bird_peers_dir)

    if kind == "bgp":
        files.append(bird_peers_dir / f"{ifname}.conf")
    elif kind == "ibgp":
        if ibgp_name is None:
            raise ValueError("ibgp_name is required for kind=ibgp")
        files.append(bird_peers_dir / f"ibgp_{ibgp_name}.conf")
        files.append(Path(config.bird_babel_conf_path))

    if net_backend == "networkd":
        netdir = Path(config.networkd_dir)
        files.extend([netdir / f"{ifname}.netdev", netdir / f"{ifname}.network"])
    elif net_backend == "nm":
        nmdir = Path(config.nm_system_connections_dir)
        files.append(nmdir / f"{ifname}.nmconnection")

    return files


def _unlink_best_effort(path: Path) -> bool:
    """Return True if deleted, False if missing."""
    try:
        path.unlink(missing_ok=True)
        return True
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法删除 {path}。{permission_hint()}") from exc
    except IsADirectoryError as exc:
        raise Dn42CtlError(f"删除失败: {path} 是目录") from exc
    except OSError as exc:
        raise Dn42CtlError(f"删除失败: {path} ({exc})") from exc


def delete_files_and_collect_status(
    files: list[Path],
) -> tuple[list[str], list[str]]:
    """Delete each path in *files*; return (deleted, missing) string lists."""
    deleted: list[str] = []
    missing: list[str] = []
    for p in files:
        existed = False
        try:
            existed = p.exists()
        except OSError:
            existed = False
        _unlink_best_effort(p)
        if existed:
            deleted.append(str(p))
        else:
            missing.append(str(p))
    return deleted, missing


def regenerate_babel_conf(*, config: AppConfig, db: Database, node_id: str) -> Path:
    try:
        interfaces = [
            (str(r["ifname"]), int(r["babel_rxcost"]), str(r["babel_type"]) if r["babel_type"] else "tunnel")
            for r in db.list_ibgp_peers(node_id)
        ]
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    babel_text = render_babel_conf(interfaces=interfaces)
    babel_path = Path(config.bird_babel_conf_path)
    write_text(babel_path, babel_text)
    return babel_path


def resolve_wg_keypair(wg_private_key: str | None, wg_public_key: str | None) -> tuple[str, str]:
    if wg_private_key is None and wg_public_key is None:
        try:
            return generate_wg_keypair()
        except WireGuardError as exc:
            raise Dn42CtlError(str(exc)) from exc
    elif wg_private_key is not None and wg_public_key is not None:
        return wg_private_key, wg_public_key
    else:
        raise Dn42CtlError("内部错误: 必须同时提供 wg_private_key 与 wg_public_key")


def open_db_and_ensure_node(db_path: Path, node_id: str) -> Database:
    db = open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    return db


def parse_allowed_ips_json(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_ALLOWED_IPS
    try:
        loaded: object = json.loads(raw)
    except json.JSONDecodeError:
        return DEFAULT_ALLOWED_IPS
    if isinstance(loaded, list):
        ips: list[str] = []
        for item in cast(list[object], loaded):
            if not isinstance(item, str):
                return DEFAULT_ALLOWED_IPS
            ips.append(item)
        return ips
    return DEFAULT_ALLOWED_IPS


def write_bird_bgp_peer(*, config: AppConfig, ifname: str, peer_lla: str, peer_asn: int, generated: list[Path]) -> None:
    bird_peer_path = Path(config.bird_peers_dir) / f"{ifname}.conf"
    try:
        bird_conf_text = render_bird_bgp_peer_conf(ifname=ifname, peer_lla=peer_lla, peer_asn=peer_asn)
    except ValueError as exc:
        raise Dn42CtlError(str(exc)) from exc
    write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)
