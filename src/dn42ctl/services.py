from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

from dn42ctl.config import AppConfig, save_config
from dn42ctl.db import BgpPeerRecord, Database, DatabaseError, IbgpPeerRecord
from dn42ctl.render import (
    load_template,
    nm_uuid_for,
    render_babel_conf,
    render_bird_bgp_peer_conf,
    render_bird_ibgp_peer_conf,
    render_bird_main_conf,
    render_networkd_netdev,
    render_networkd_network,
    render_nmconnection_wireguard,
)
from dn42ctl.wg import WireGuardError, generate_random_lla_cidr, generate_wg_keypair


DEFAULT_ALLOWED_IPS = ["fe80::/64", "fd00::/8"]


class Dn42CtlError(RuntimeError):
    pass


def _permission_hint() -> str:
    return (
        "请确认当前用户对该路径有写权限；"
        "若使用默认系统路径，通常需要以 root 运行（sudo）。"
        "也可以通过 --config-path/--db-path 或 init 的路径参数覆盖输出目录。"
    )


@dataclass(frozen=True)
class InitResult:
    config: AppConfig
    config_path: Path
    db_path: Path
    bird_conf_path: Path
    bird_babel_conf_path: Path


@dataclass(frozen=True)
class PeerResult:
    ifname: str
    listen_port: int
    wg_public_key: str
    local_lla: str
    generated_files: list[Path]


def _chmod_if_possible(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法写入 {path}。{_permission_hint()}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"写入失败: {path} ({exc})") from exc
    if mode is not None:
        _chmod_if_possible(path, mode)


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise Dn42CtlError(
            f"权限不足: 无法创建目录 {path}。{_permission_hint()}"
        ) from exc
    except OSError as exc:
        raise Dn42CtlError(f"创建目录失败: {path} ({exc})") from exc


def _open_db(db_path: Path) -> Database:
    try:
        return Database.open(db_path)
    except PermissionError as exc:
        raise Dn42CtlError(
            f"权限不足: 无法创建/写入数据库 {db_path}。{_permission_hint()}"
        ) from exc
    except OSError as exc:
        raise Dn42CtlError(f"打开数据库失败: {db_path} ({exc})") from exc
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc


def _pick_unused_port(used: set[int]) -> int:
    # Keep away from the well-known/default WG port; prefer high ports.
    candidate = random.randint(20000, 65535)
    attempts = 0
    while candidate in used:
        candidate = random.randint(20000, 65535)
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


def _write_net_backend_files(
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
        _write_text(
            netdev_path,
            render_networkd_netdev(
                ifname=ifname,
                private_key=private_key,
                listen_port=listen_port,
                peer_public_key=peer_public_key,
                endpoint=endpoint,
                allowed_ips=allowed_ips,
            ),
            mode=0o600,
        )
        _write_text(
            network_path,
            render_networkd_network(
                ifname=ifname,
                local_lla_cidr=local_lla,
                peer_lla=peer_lla,
            ),
        )
        generated.extend([netdev_path, network_path])
    elif backend == "nm":
        nm_path = Path(config.nm_system_connections_dir) / f"{ifname}.nmconnection"
        _write_text(
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
                local_ipv6_cidr=local_lla,
            ),
            mode=0o600,
        )
        generated.append(nm_path)


def normalize_net_backend(net_backend: str) -> str:
    backend = net_backend.strip().lower()
    if backend == "networkd":
        return "networkd"
    if backend in {"nm", "networkmanager"}:
        return "nm"
    raise Dn42CtlError("net_backend 必须是 networkd 或 nm")


def init_node(
    *,
    config_path: Path,
    db_path: Path,
    node_id: str,
    own_asn: int,
    router_id: str,
    own_ipv6: str,
    ownnet_v6: str,
    ownnetset_v6: str,
    bird_conf_path: Path,
    bird_peers_dir: Path,
    bird_babel_conf_path: Path,
    bird_roa_v6_conf_path: Path,
    networkd_dir: Path,
    nm_system_connections_dir: Path,
    overwrite_bird_conf: bool,
    overwrite_babel_conf: bool,
) -> InitResult:
    config = AppConfig(
        node_id=node_id,
        own_asn=own_asn,
        router_id=router_id,
        own_ipv6=own_ipv6,
        ownnet_v6=ownnet_v6,
        ownnetset_v6=ownnetset_v6,
        bird_conf_path=str(bird_conf_path),
        bird_peers_dir=str(bird_peers_dir),
        bird_babel_conf_path=str(bird_babel_conf_path),
        bird_roa_v6_conf_path=str(bird_roa_v6_conf_path),
        networkd_dir=str(networkd_dir),
        nm_system_connections_dir=str(nm_system_connections_dir),
    )
    try:
        save_config(config_path, config)
    except PermissionError as exc:
        raise Dn42CtlError(
            f"权限不足: 无法写入配置 {config_path}。{_permission_hint()}"
        ) from exc
    except OSError as exc:
        raise Dn42CtlError(f"写入配置失败: {config_path} ({exc})") from exc

    db = _open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    template_text = load_template("bird.conf_template")
    bird_conf_text = render_bird_main_conf(
        template_text=template_text,
        own_asn=own_asn,
        router_id=router_id,
        own_ipv6=own_ipv6,
        ownnet_v6=ownnet_v6,
        ownnetset_v6=ownnetset_v6,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_peers_dir=bird_peers_dir,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
    )

    if bird_conf_path.exists() and not overwrite_bird_conf:
        raise Dn42CtlError(f"Bird 主配置已存在且未允许覆盖: {bird_conf_path}")
    _write_text(bird_conf_path, bird_conf_text)

    _ensure_dir(bird_peers_dir)

    babel_text = render_babel_conf(interface_names=[])
    if bird_babel_conf_path.exists() and not overwrite_babel_conf:
        raise Dn42CtlError(f"babel.conf 已存在且未允许覆盖: {bird_babel_conf_path}")
    _write_text(bird_babel_conf_path, babel_text)

    return InitResult(
        config=config,
        config_path=config_path,
        db_path=db_path,
        bird_conf_path=bird_conf_path,
        bird_babel_conf_path=bird_babel_conf_path,
    )


def create_bgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    peer_asn: int,
    peer_public_key: str,
    endpoint: str,
    peer_lla: str,
    net_backend: str,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = config.node_id
    db = _open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    try:
        if db.get_bgp_peer(node_id, peer_asn) is not None:
            raise Dn42CtlError("该 BGP peer 已存在，请使用 bgp peer modify")
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    as_str = str(peer_asn)
    as_last4 = as_str[-4:]
    as_last5 = as_str[-5:]

    ifname = f"dn42_{as_last4}"
    listen_port = int(as_last5)
    if listen_port > 65535:
        raise Dn42CtlError(f"由 ASN 推导的 ListenPort 超出范围: {listen_port}")

    try:
        private_key, public_key = generate_wg_keypair()
    except WireGuardError as exc:
        raise Dn42CtlError(str(exc)) from exc

    local_lla = generate_random_lla_cidr()
    allowed_ips = DEFAULT_ALLOWED_IPS

    try:
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=node_id,
                peer_asn=peer_asn,
                ifname=ifname,
                wg_private_key=private_key,
                wg_public_key=public_key,
                peer_public_key=peer_public_key,
                endpoint=endpoint,
                local_lla=local_lla,
                peer_lla=peer_lla,
                listen_port=listen_port,
                allowed_ips=allowed_ips,
                net_backend=backend,
            )
        )
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    generated: list[Path] = []

    bird_peer_path = Path(config.bird_peers_dir) / f"{ifname}.conf"
    try:
        bird_conf_text = render_bird_bgp_peer_conf(
            ifname=ifname, peer_lla=peer_lla, peer_asn=peer_asn
        )
    except ValueError as exc:
        raise Dn42CtlError(str(exc)) from exc
    _write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    _write_net_backend_files(
        config=config,
        node_id=node_id,
        backend=backend,
        ifname=ifname,
        private_key=private_key,
        listen_port=listen_port,
        peer_public_key=peer_public_key,
        endpoint=endpoint,
        allowed_ips=allowed_ips,
        local_lla=local_lla,
        peer_lla=peer_lla,
        generated=generated,
    )

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla,
        generated_files=generated,
    )


def modify_bgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    peer_asn: int,
    peer_public_key: str,
    endpoint: str,
    peer_lla: str,
    net_backend: str,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = config.node_id
    db = _open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    try:
        row = db.get_bgp_peer(node_id, peer_asn)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if row is None:
        raise Dn42CtlError("该 BGP peer 不存在")

    ifname = str(row["ifname"])
    private_key = str(row["wg_private_key"])
    public_key = str(row["wg_public_key"])
    listen_port = int(row["listen_port"])
    local_lla = str(row["local_lla"])
    # Restore the stored allowed_ips instead of silently falling back to DEFAULT;
    # this prevents overwriting user-customised AllowedIPs on every modify.
    raw_ips = row["allowed_ips_json"]
    allowed_ips: list[str] = json.loads(raw_ips) if raw_ips else DEFAULT_ALLOWED_IPS

    try:
        db.update_bgp_peer(
            node_id=node_id,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            allowed_ips=allowed_ips,
            net_backend=backend,
        )
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    generated: list[Path] = []
    bird_peer_path = Path(config.bird_peers_dir) / f"{ifname}.conf"
    try:
        bird_conf_text = render_bird_bgp_peer_conf(
            ifname=ifname, peer_lla=peer_lla, peer_asn=peer_asn
        )
    except ValueError as exc:
        raise Dn42CtlError(str(exc)) from exc
    _write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    _write_net_backend_files(
        config=config,
        node_id=node_id,
        backend=backend,
        ifname=ifname,
        private_key=private_key,
        listen_port=listen_port,
        peer_public_key=peer_public_key,
        endpoint=endpoint,
        allowed_ips=allowed_ips,
        local_lla=local_lla,
        peer_lla=peer_lla,
        generated=generated,
    )

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla,
        generated_files=generated,
    )


def create_ibgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    name: str,
    peer_public_key: str,
    endpoint: str,
    peer_lla: str,
    net_backend: str,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = config.node_id
    db = _open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    peer_name = sanitize_name(name)
    ifname = f"wg_{peer_name}"
    if len(ifname) > 15:
        raise Dn42CtlError("接口名过长，请使用更短的 name")

    try:
        used_ports = db.get_used_listen_ports(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    listen_port = _pick_unused_port(used_ports)

    try:
        private_key, public_key = generate_wg_keypair()
    except WireGuardError as exc:
        raise Dn42CtlError(str(exc)) from exc

    local_lla = generate_random_lla_cidr()
    allowed_ips = DEFAULT_ALLOWED_IPS

    try:
        db.insert_ibgp_peer(
            IbgpPeerRecord(
                node_id=node_id,
                name=peer_name,
                ifname=ifname,
                wg_private_key=private_key,
                wg_public_key=public_key,
                peer_public_key=peer_public_key,
                endpoint=endpoint,
                local_lla=local_lla,
                peer_lla=peer_lla,
                listen_port=listen_port,
                allowed_ips=allowed_ips,
                net_backend=backend,
            )
        )
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    generated: list[Path] = []

    bird_peer_path = Path(config.bird_peers_dir) / f"ibgp_{peer_name}.conf"
    try:
        bird_conf_text = render_bird_ibgp_peer_conf(
            name=peer_name, ifname=ifname, peer_lla=peer_lla
        )
    except ValueError as exc:
        raise Dn42CtlError(str(exc)) from exc
    _write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    _write_net_backend_files(
        config=config,
        node_id=node_id,
        backend=backend,
        ifname=ifname,
        private_key=private_key,
        listen_port=listen_port,
        peer_public_key=peer_public_key,
        endpoint=endpoint,
        allowed_ips=allowed_ips,
        local_lla=local_lla,
        peer_lla=peer_lla,
        generated=generated,
    )

    # Regenerate babel.conf deterministically from DB.
    try:
        interface_names = [str(r["ifname"]) for r in db.list_ibgp_peers(node_id)]
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    babel_text = render_babel_conf(interface_names=interface_names)
    babel_path = Path(config.bird_babel_conf_path)
    _write_text(babel_path, babel_text)
    generated.append(babel_path)

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla,
        generated_files=generated,
    )
