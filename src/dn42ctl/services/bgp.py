from __future__ import annotations

from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.constants import MAX_PORT
from dn42ctl.db import BgpPeerRecord, DatabaseError
from dn42ctl.services.core import (
    DEFAULT_ALLOWED_IPS,
    DeleteResult,
    Dn42CtlError,
    PeerResult,
    delete_files_and_collect_status,
    normalize_net_backend,
    open_db,
    open_db_and_ensure_node,
    parse_allowed_ips_json,
    peer_files_for_backend,
    resolve_wg_keypair,
    write_bird_bgp_peer,
    write_net_backend_files,
)
from dn42ctl.validators import ValidationError, validate_listen_port
from dn42ctl.wg import generate_random_lla


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
    node_id: str | None = None,
    render_files: bool = True,
) -> PeerResult:
    """Create a BGP peer.

    - `node_id` overrides `config.node_id`. Use this when writing on behalf of a
      remote node (e.g. accept_proposal / import_report on the central server).
    - `render_files=False` skips writing Bird / network backend files. The server
      process is sandbox-restricted and cannot touch /etc/bird etc.; only the DB
      row is created.
    """
    backend = normalize_net_backend(net_backend)

    node_id = node_id or config.node_id
    db = open_db_and_ensure_node(db_path, node_id)

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
        if listen_port > MAX_PORT:
            raise Dn42CtlError(f"由 ASN 推导的 ListenPort 超出范围: {listen_port}")
    else:
        try:
            validate_listen_port(listen_port, allow_zero=True)
        except ValidationError as exc:
            raise Dn42CtlError(str(exc)) from exc

    private_key, public_key = resolve_wg_keypair(wg_private_key, wg_public_key)

    local_lla_addr = local_lla or generate_random_lla()
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
                local_lla=local_lla_addr,
                peer_lla=peer_lla,
                listen_port=listen_port,
                allowed_ips=allowed_ips,
                net_backend=backend,
            )
        )
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    generated: list[Path] = []

    if render_files:
        write_bird_bgp_peer(config=config, ifname=ifname, peer_lla=peer_lla, peer_asn=peer_asn, generated=generated)
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
            local_lla=local_lla_addr,
            peer_lla=peer_lla,
            generated=generated,
        )

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla_addr,
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
    node_id: str | None = None,
    render_files: bool = True,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    node_id = node_id or config.node_id
    db = open_db_and_ensure_node(db_path, node_id)

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
    try:
        validate_listen_port(new_listen_port, allow_zero=True)
    except ValidationError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if listen_port is not None and new_listen_port > 0 and new_listen_port != current_listen_port:
        try:
            used_ports = db.get_used_listen_ports(node_id)
        except DatabaseError as exc:
            raise Dn42CtlError(str(exc)) from exc
        used_ports.discard(0)
        used_ports.discard(current_listen_port)
        if new_listen_port in used_ports:
            raise Dn42CtlError(f"ListenPort 已被占用: {new_listen_port}")
    local_lla = str(row["local_lla"])
    allowed_ips = parse_allowed_ips_json(row["allowed_ips_json"])

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
    if render_files:
        write_bird_bgp_peer(config=config, ifname=ifname, peer_lla=peer_lla, peer_asn=peer_asn, generated=generated)
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
    node_id: str | None = None,
    render_files: bool = True,
) -> DeleteResult:
    db = open_db(db_path)
    node_id = node_id or config.node_id
    try:
        row = db.get_bgp_peer(node_id, peer_asn)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if row is None:
        raise Dn42CtlError("该 BGP peer 不存在")

    ifname = str(row["ifname"])
    net_backend = str(row["net_backend"])

    deleted: list[str] = []
    missing: list[str] = []
    if render_files:
        files = peer_files_for_backend(config=config, ifname=ifname, net_backend=net_backend, kind="bgp")
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
