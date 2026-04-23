from __future__ import annotations

from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.constants import MAX_PORT
from dn42ctl.db import DatabaseError, IbgpPeerRecord
from dn42ctl.render import render_bird_ibgp_peer_conf
from dn42ctl.wg import generate_random_lla_cidr

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
    pick_unused_port,
    regenerate_babel_conf,
    resolve_wg_keypair,
    sanitize_name,
    write_net_backend_files,
    write_text,
)


def create_ibgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    name: str,
    peer_ip: str,
    has_wg: bool = True,
    peer_public_key: str | None = None,
    endpoint: str | None = None,
    peer_lla: str | None = None,
    net_backend: str | None = None,
    babel_rxcost: int = 0,
    listen_port: int | None = None,
    wg_private_key: str | None = None,
    wg_public_key: str | None = None,
    local_lla: str | None = None,
) -> PeerResult:
    node_id = config.node_id
    db = open_db_and_ensure_node(db_path, node_id)

    peer_name = sanitize_name(name)
    ifname = f"wg_{peer_name}"
    if len(ifname) > 15:
        raise Dn42CtlError("接口名过长，请使用更短的 name")

    if has_wg:
        backend = normalize_net_backend(net_backend or "networkd")

        if listen_port is None:
            try:
                used_ports = db.get_used_listen_ports(node_id)
            except DatabaseError as exc:
                raise Dn42CtlError(str(exc)) from exc
            used_ports.discard(0)
            listen_port = pick_unused_port(used_ports)
        else:
            if listen_port < 0 or listen_port > MAX_PORT:
                raise Dn42CtlError(f"ListenPort 超出范围 (0/1-{MAX_PORT}): {listen_port}")
            if listen_port > 0:
                try:
                    used_ports = db.get_used_listen_ports(node_id)
                except DatabaseError as exc:
                    raise Dn42CtlError(str(exc)) from exc
                used_ports.discard(0)
                if listen_port in used_ports:
                    raise Dn42CtlError(f"ListenPort 已被占用: {listen_port}")

        if babel_rxcost < 0 or babel_rxcost > MAX_PORT:
            raise Dn42CtlError(f"rxcost 超出范围 (0-{MAX_PORT}): {babel_rxcost}")

        private_key, public_key = resolve_wg_keypair(wg_private_key, wg_public_key)
        local_lla_cidr = local_lla or generate_random_lla_cidr()
        allowed_ips = DEFAULT_ALLOWED_IPS
    else:
        backend = "networkd"
        listen_port = 0
        private_key = ""
        public_key = ""
        local_lla_cidr = ""
        allowed_ips = DEFAULT_ALLOWED_IPS

    try:
        db.insert_ibgp_peer(
            IbgpPeerRecord(
                node_id=node_id,
                name=peer_name,
                ifname=ifname,
                wg_private_key=private_key,
                wg_public_key=public_key,
                peer_public_key=peer_public_key or "",
                endpoint=endpoint or "",
                local_lla=local_lla_cidr,
                peer_lla=peer_lla or "",
                listen_port=listen_port,
                allowed_ips=allowed_ips,
                net_backend=backend,
                babel_rxcost=babel_rxcost,
                peer_ip=peer_ip,
                has_wg=has_wg,
            )
        )
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    generated: list[Path] = []

    bird_peer_path = Path(config.bird_peers_dir) / f"ibgp_{peer_name}.conf"
    try:
        bird_conf_text = render_bird_ibgp_peer_conf(
            name=peer_name, ifname=ifname, peer_ip=peer_ip
        )
    except ValueError as exc:
        raise Dn42CtlError(str(exc)) from exc
    write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    if has_wg:
        write_net_backend_files(
            config=config,
            node_id=node_id,
            backend=backend,
            ifname=ifname,
            private_key=private_key,
            listen_port=listen_port,
            peer_public_key=peer_public_key or "",
            endpoint=endpoint or "",
            allowed_ips=allowed_ips,
            local_lla=local_lla_cidr,
            peer_lla=peer_lla or "",
            generated=generated,
        )

        babel_path = regenerate_babel_conf(config=config, db=db, node_id=node_id)
        generated.append(babel_path)

    return PeerResult(
        ifname=ifname,
        listen_port=listen_port,
        wg_public_key=public_key,
        local_lla=local_lla_cidr,
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
    row_has_wg = bool(row["has_wg"])

    if row_has_wg:
        files = peer_files_for_backend(
            config=config,
            ifname=ifname,
            net_backend=net_backend,
            kind="ibgp",
            ibgp_name=peer_name,
        )
        babel_path = Path(config.bird_babel_conf_path)
        files = [p for p in files if p != babel_path]
    else:
        bird_peer_path = Path(config.bird_peers_dir) / f"ibgp_{peer_name}.conf"
        files = [bird_peer_path]

    deleted, missing = delete_files_and_collect_status(files)

    try:
        db.delete_ibgp_peer(node_id, peer_name)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    regenerated: list[str] = []
    if row_has_wg:
        babel_path = Path(config.bird_babel_conf_path)
        regenerate_babel_conf(config=config, db=db, node_id=node_id)
        regenerated.append(str(babel_path))

    return DeleteResult(
        kind="ibgp",
        peer_asn=None,
        name=peer_name,
        deleted_files=deleted,
        missing_files=missing,
        regenerated_files=regenerated,
    )


def modify_ibgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    name: str,
    peer_public_key: str,
    endpoint: str,
    peer_lla: str,
    net_backend: str,
    babel_rxcost: int,
    peer_ip: str,
    listen_port: int | None = None,
) -> PeerResult:
    backend = normalize_net_backend(net_backend)

    if babel_rxcost < 0 or babel_rxcost > MAX_PORT:
        raise Dn42CtlError(f"rxcost 超出范围 (0-{MAX_PORT}): {babel_rxcost}")

    node_id = config.node_id
    db = open_db_and_ensure_node(db_path, node_id)

    peer_name = sanitize_name(name)
    try:
        row = db.get_ibgp_peer(node_id, peer_name)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if row is None:
        raise Dn42CtlError("该 iBGP peer 不存在")

    if not bool(row["has_wg"]):
        raise Dn42CtlError("该 iBGP peer 未启用 WireGuard，不支持修改 WG 相关参数")

    ifname = str(row["ifname"])
    private_key = str(row["wg_private_key"])
    public_key = str(row["wg_public_key"])
    local_lla = str(row["local_lla"])
    current_listen_port = int(row["listen_port"])
    new_listen_port = current_listen_port if listen_port is None else listen_port
    if new_listen_port < 0 or new_listen_port > MAX_PORT:
        raise Dn42CtlError(f"ListenPort 超出范围 (0/1-{MAX_PORT}): {new_listen_port}")
    if (
        listen_port is not None
        and new_listen_port > 0
        and new_listen_port != current_listen_port
    ):
        try:
            used_ports = db.get_used_listen_ports(node_id)
        except DatabaseError as exc:
            raise Dn42CtlError(str(exc)) from exc
        used_ports.discard(0)
        used_ports.discard(current_listen_port)
        if new_listen_port in used_ports:
            raise Dn42CtlError(f"ListenPort 已被占用: {new_listen_port}")
    allowed_ips = parse_allowed_ips_json(row["allowed_ips_json"])

    try:
        db.update_ibgp_peer(
            node_id=node_id,
            name=peer_name,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            listen_port=new_listen_port,
            allowed_ips=allowed_ips,
            net_backend=backend,
            babel_rxcost=babel_rxcost,
            peer_ip=peer_ip,
        )
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    generated: list[Path] = []

    bird_peer_path = Path(config.bird_peers_dir) / f"ibgp_{peer_name}.conf"
    try:
        bird_conf_text = render_bird_ibgp_peer_conf(
            name=peer_name, ifname=ifname, peer_ip=peer_ip
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

    babel_path = regenerate_babel_conf(config=config, db=db, node_id=node_id)
    generated.append(babel_path)

    return PeerResult(
        ifname=ifname,
        listen_port=new_listen_port,
        wg_public_key=public_key,
        local_lla=local_lla,
        generated_files=generated,
    )
