from __future__ import annotations

from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.db import DatabaseError, IbgpPeerRecord
from dn42ctl.render import render_babel_conf, render_bird_ibgp_peer_conf
from dn42ctl.wg import WireGuardError, generate_random_lla_cidr, generate_wg_keypair

from dn42ctl.services.core import (
    DEFAULT_ALLOWED_IPS,
    DeleteResult,
    Dn42CtlError,
    PeerResult,
    open_db,
    pick_unused_port,
    write_net_backend_files,
    write_text,
    delete_files_and_collect_status,
    normalize_net_backend,
    peer_files_for_backend,
    sanitize_name,
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
    listen_port: int | None = None,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = config.node_id
    db = open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    peer_name = sanitize_name(name)
    ifname = f"wg_{peer_name}"
    if len(ifname) > 15:
        raise Dn42CtlError("接口名过长，请使用更短的 name")

    if listen_port is None:
        try:
            used_ports = db.get_used_listen_ports(node_id)
        except DatabaseError as exc:
            raise Dn42CtlError(str(exc)) from exc
        used_ports.discard(0)
        listen_port = pick_unused_port(used_ports)
    else:
        if listen_port < 0 or listen_port > 65535:
            raise Dn42CtlError(f"ListenPort 超出范围 (0/1-65535): {listen_port}")
        if listen_port > 0:
            try:
                used_ports = db.get_used_listen_ports(node_id)
            except DatabaseError as exc:
                raise Dn42CtlError(str(exc)) from exc
            used_ports.discard(0)
            if listen_port in used_ports:
                raise Dn42CtlError(f"ListenPort 已被占用: {listen_port}")

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
    write_text(babel_path, babel_text)
    generated.append(babel_path)

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla,
        generated_files=generated,
    )


def delete_ibgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    name: str,
) -> DeleteResult:
    db = open_db(db_path)
    node_id = config.node_id

    peer_name = sanitize_name(name)
    try:
        row = db.get_ibgp_peer(node_id, peer_name)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if row is None:
        raise Dn42CtlError("该 iBGP peer 不存在")

    ifname = str(row["ifname"])
    net_backend = str(row["net_backend"])
    files = peer_files_for_backend(
        config=config,
        ifname=ifname,
        net_backend=net_backend,
        kind="ibgp",
        ibgp_name=peer_name,
    )
    babel_path = Path(config.bird_babel_conf_path)
    files = [p for p in files if p != babel_path]

    deleted, missing = delete_files_and_collect_status(files)

    try:
        db.delete_ibgp_peer(node_id, peer_name)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    # Regenerate babel.conf deterministically from DB.
    try:
        interface_names = [str(r["ifname"]) for r in db.list_ibgp_peers(node_id)]
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    babel_text = render_babel_conf(interface_names=interface_names)
    write_text(babel_path, babel_text)

    return DeleteResult(
        kind="ibgp",
        peer_asn=None,
        name=peer_name,
        deleted_files=deleted,
        missing_files=missing,
        regenerated_files=[str(babel_path)],
    )
