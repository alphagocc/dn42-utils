from __future__ import annotations

import json
from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.db import BgpPeerRecord, DatabaseError
from dn42ctl.render import render_bird_bgp_peer_conf
from dn42ctl.wg import WireGuardError, generate_random_lla_cidr, generate_wg_keypair

from dn42ctl.services.core import (
    DEFAULT_ALLOWED_IPS,
    DeleteResult,
    Dn42CtlError,
    PeerResult,
    open_db,
    write_net_backend_files,
    write_text,
    delete_files_and_collect_status,
    normalize_net_backend,
    peer_files_for_backend,
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
    listen_port: int | None = None,
    wg_private_key: str | None = None,
    wg_public_key: str | None = None,
    local_lla: str | None = None,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = config.node_id
    db = open_db(db_path)
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
    if listen_port is None:
        listen_port = int(as_last5)
        if listen_port > 65535:
            raise Dn42CtlError(f"由 ASN 推导的 ListenPort 超出范围: {listen_port}")
    else:
        if listen_port < 0 or listen_port > 65535:
            raise Dn42CtlError(f"ListenPort 超出范围 (0/1-65535): {listen_port}")

    if wg_private_key is None and wg_public_key is None:
        try:
            private_key, public_key = generate_wg_keypair()
        except WireGuardError as exc:
            raise Dn42CtlError(str(exc)) from exc
    elif wg_private_key is not None and wg_public_key is not None:
        private_key = wg_private_key
        public_key = wg_public_key
    else:
        raise Dn42CtlError("内部错误: 必须同时提供 wg_private_key 与 wg_public_key")

    local_lla_cidr = local_lla or generate_random_lla_cidr()
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
                local_lla=local_lla_cidr,
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
    write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    write_net_backend_files(
        config=config,
        node_id=node_id,
        backend=backend,
        ifname=ifname,
        private_key=private_key,
        listen_port=listen_port,
        peer_public_key=peer_public_key,
        endpoint=endpoint,
        allowed_ips=allowed_ips,
        local_lla=local_lla_cidr,
        peer_lla=peer_lla,
        generated=generated,
    )

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla_cidr,
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
    listen_port: int | None = None,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = config.node_id
    db = open_db(db_path)
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
    current_listen_port = int(row["listen_port"])
    new_listen_port = current_listen_port if listen_port is None else listen_port
    if new_listen_port < 0 or new_listen_port > 65535:
        raise Dn42CtlError(f"ListenPort 超出范围 (0/1-65535): {new_listen_port}")
    if (
        listen_port is not None
        and new_listen_port > 0
        and new_listen_port != current_listen_port
    ):
        # Avoid port conflicts within this node (best-effort).
        try:
            used_ports = db.get_used_listen_ports(node_id)
        except DatabaseError as exc:
            raise Dn42CtlError(str(exc)) from exc
        used_ports.discard(0)
        used_ports.discard(current_listen_port)
        if new_listen_port in used_ports:
            raise Dn42CtlError(f"ListenPort 已被占用: {new_listen_port}")
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
            listen_port=new_listen_port,
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
    write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    write_net_backend_files(
        config=config,
        node_id=node_id,
        backend=backend,
        ifname=ifname,
        private_key=private_key,
        listen_port=new_listen_port,
        peer_public_key=peer_public_key,
        endpoint=endpoint,
        allowed_ips=allowed_ips,
        local_lla=local_lla,
        peer_lla=peer_lla,
        generated=generated,
    )

    return PeerResult(
        ifname=ifname,
        listen_port=new_listen_port,
        wg_public_key=public_key,
        local_lla=local_lla,
        generated_files=generated,
    )


def delete_bgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    peer_asn: int,
) -> DeleteResult:
    db = open_db(db_path)
    node_id = config.node_id
    try:
        row = db.get_bgp_peer(node_id, peer_asn)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if row is None:
        raise Dn42CtlError("该 BGP peer 不存在")

    ifname = str(row["ifname"])
    net_backend = str(row["net_backend"])
    files = peer_files_for_backend(
        config=config, ifname=ifname, net_backend=net_backend, kind="bgp"
    )

    deleted, missing = delete_files_and_collect_status(files)

    try:
        db.delete_bgp_peer(node_id, peer_asn)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    return DeleteResult(
        kind="bgp",
        peer_asn=peer_asn,
        name=None,
        deleted_files=deleted,
        missing_files=missing,
        regenerated_files=[],
    )
