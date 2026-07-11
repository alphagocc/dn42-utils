from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import typer

from dn42ctl.config import AppConfig, ConfigError, save_config
from dn42ctl.context import AppContext
from dn42ctl.db import Database, DatabaseError
from dn42ctl.paths import (
    DEFAULT_BIRD_BABEL_CONF_PATH,
    DEFAULT_BIRD_CONF_PATH,
    DEFAULT_BIRD_PEERS_DIR,
    DEFAULT_BIRD_ROA_V6_CONF_PATH,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_NETWORKD_DIR,
    DEFAULT_NM_SYSTEM_CONNECTIONS_DIR,
)
from dn42ctl.services import (
    Dn42CtlError,
    create_bgp_peer,
    create_ibgp_peer,
    delete_bgp_peer,
    delete_ibgp_peer,
    discover_bird_paths,
    genconf,
    init_node,
    modify_bgp_peer,
    modify_ibgp_peer,
    scan_local_configs,
    show_bgp_peers,
    show_ibgp_peers,
    show_wg_tunnels,
)
from dn42ctl.services.core import BgpPeerView, IbgpPeerView, WgTunnelView, parse_allowed_ips_json, sanitize_name
from dn42ctl.validators import (
    ValidationError as _ValidationError,
)
from dn42ctl.validators import (
    validate_allowed_ips,
    validate_asn,
    validate_babel_type,
    validate_endpoint,
    validate_ipv6_address,
    validate_ipv6_network,
    validate_net_backend,
    validate_ownnetset_v6,
    validate_pubkey,
    validate_router_id,
    validate_rxcost,
)
from dn42ctl.wg import WireGuardError, generate_random_lla, generate_wg_keypair

app = typer.Typer(add_completion=False)


def _db_open_hint(db_path: Path) -> str:
    return (
        f"无法打开数据库: {db_path}。"
        "若使用默认系统路径，通常需要以 root 运行（sudo），"
        "或使用 --db-path 覆盖到可写位置。"
    )


def _cli_validate(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except _ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _default_router_id() -> str:
    a = secrets.randbelow(254) + 1
    b = secrets.randbelow(254) + 1
    return f"169.254.{a}.{b}"


def _print_dummy_result(dummy: object | None) -> None:
    if dummy is None:
        return
    from dn42ctl.services.dummy import DummyResult

    if not isinstance(dummy, DummyResult):
        return
    if dummy.skipped:
        typer.echo("dn42-dummy: 已存在，跳过")
    elif dummy.created:
        typer.echo(f"dn42-dummy: 已创建 (backend={dummy.backend})")
    for w in dummy.warnings:
        typer.echo(f"  警告: {w}")


def _require_config_or_exit(appctx: AppContext) -> AppConfig:
    try:
        return appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc


def _open_db_or_exit(appctx: AppContext) -> "Database":
    try:
        return appctx.open_db()
    except (PermissionError, OSError) as exc:
        typer.echo(f"错误: 权限不足/路径不可写 ({exc})")
        typer.echo(_db_open_hint(appctx.db_path))
        raise typer.Exit(1) from exc
    except DatabaseError as exc:
        typer.echo(f"错误: {exc}")
        typer.echo(_db_open_hint(appctx.db_path))
        raise typer.Exit(1) from exc


def _prepare_peer_info(
    peer_public_key: str | None,
    endpoint: str | None,
    peer_lla: str | None,
    *,
    allow_empty_endpoint: bool = False,
) -> tuple[str | None, str | None, str | None, str, str, str]:
    prepared_private_key: str | None = None
    prepared_public_key: str | None = None
    prepared_local_lla: str | None = None
    if peer_public_key is None or endpoint is None or peer_lla is None:
        try:
            prepared_private_key, prepared_public_key = generate_wg_keypair()
            prepared_local_lla = generate_random_lla()
        except WireGuardError as exc:
            typer.echo(f"错误: {exc}")
            raise typer.Exit(1) from exc

        typer.echo("本端信息（无需对端提供，先发给对端）：")
        typer.echo(f"本端 WG 公钥: {prepared_public_key}")
        typer.echo(f"本端 LLA: {prepared_local_lla}")

    if peer_public_key is None:
        peer_public_key = typer.prompt("Peer 公钥")
    if endpoint is None:
        if allow_empty_endpoint:
            endpoint = typer.prompt("Peer Endpoint (IP:Port，留空跳过)", default="")
        else:
            endpoint = typer.prompt("Peer Endpoint (IP:Port)")
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA (fe80::...)")

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None

    return (
        prepared_private_key,
        prepared_public_key,
        prepared_local_lla,
        peer_public_key,
        endpoint,
        peer_lla,
    )


def _confirm_overwrite_if_exists(path: Path) -> bool:
    if path.exists():
        return typer.confirm(f"{path} 已存在，覆盖？", default=False)
    return True


def _print_wg_tunnels(tunnels: list["WgTunnelView"]) -> None:
    if not tunnels:
        typer.echo("(空) 未找到任何 WireGuard 隧道")
        return
    for t in tunnels:
        ident = f"AS{t.peer_asn}" if t.kind == "bgp" else f"{t.name}"
        typer.echo(f"[{t.kind}] {ident} ifname={t.ifname} backend={t.net_backend} port={t.listen_port}")
        typer.echo(f"  peer_pubkey: {t.peer_public_key or ''}")
        typer.echo(f"  endpoint: {t.endpoint or ''}")
        typer.echo(f"  local_lla: {t.local_lla}  peer_lla: {t.peer_lla or ''}")
        typer.echo(f"  allowed_ips: {', '.join(t.allowed_ips)}")
        _print_file_statuses([asdict(f) for f in t.files])
        if t.live_wg is not None:
            typer.echo(f"  live(wg): {'OK' if t.live_wg.ok else 'UNAVAILABLE'}")


def _print_bgp_peers(peers: list["BgpPeerView"]) -> None:
    if not peers:
        typer.echo("(空) 未找到任何 BGP peer")
        return
    for p in peers:
        typer.echo(f"AS{p.peer_asn} ifname={p.ifname} backend={p.net_backend} port={p.listen_port}")
        typer.echo(f"  peer_lla: {p.peer_lla or ''}")
        typer.echo(f"  endpoint: {p.endpoint or ''}")
        typer.echo(f"  peer_pubkey: {p.peer_public_key or ''}")
        typer.echo(f"  wg_pubkey(local): {p.wg_public_key}")
        _print_file_statuses([asdict(f) for f in p.files])
        if p.live_wg is not None:
            typer.echo(f"  live(wg): {'OK' if p.live_wg.ok else 'UNAVAILABLE'}")
        if p.live_bird is not None:
            typer.echo(f"  live(birdc): {'OK' if p.live_bird.ok else 'UNAVAILABLE'}")


def _print_ibgp_peers(peers: list["IbgpPeerView"]) -> None:
    if not peers:
        typer.echo("(空) 未找到任何 iBGP peer")
        return
    for p in peers:
        proto = f"ibgp_{p.name}"
        wg_tag = "wg" if p.has_wg else "no-wg"
        typer.echo(f"{p.name} proto={proto} ifname={p.ifname} [{wg_tag}] backend={p.net_backend} port={p.listen_port}")
        typer.echo(f"  peer_ip: {p.peer_ip or ''}")
        if p.has_wg:
            typer.echo(f"  babel_rxcost: {p.babel_rxcost}")
            typer.echo(f"  babel_type: {p.babel_type}")
            typer.echo(f"  peer_lla: {p.peer_lla or ''}")
            typer.echo(f"  endpoint: {p.endpoint or ''}")
            typer.echo(f"  peer_pubkey: {p.peer_public_key or ''}")
            typer.echo(f"  wg_pubkey(local): {p.wg_public_key}")
        _print_file_statuses([asdict(f) for f in p.files])
        if p.live_wg is not None:
            typer.echo(f"  live(wg): {'OK' if p.live_wg.ok else 'UNAVAILABLE'}")
        if p.live_bird is not None:
            typer.echo(f"  live(birdc): {'OK' if p.live_bird.ok else 'UNAVAILABLE'}")


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config-path",
        envvar="DN42CTL_CONFIG",
        help="配置文件路径 (默认 /etc/dn42ctl/config.toml)",
    ),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        envvar="DN42CTL_DB",
        help="SQLite 路径 (默认 /var/lib/dn42ctl/dn42.sqlite3)",
    ),
) -> None:
    ctx.obj = AppContext(config_path=config_path, db_path=db_path)


@app.command("init")
def cmd_init(
    ctx: typer.Context,
    own_asn: int | None = typer.Option(None, "--own-asn", help="本机 DN42 ASN (OWNAS)"),
    own_ipv6: str | None = typer.Option(
        None,
        "--own-ipv6",
        help="本机 DN42 IPv6 (支持输入 4 位 hex 作为最后一段)",
    ),
    ownnet_v6: str | None = typer.Option(None, "--ownnet-v6", help="本机 DN42 IPv6 前缀"),
    ownnetset_v6: str | None = typer.Option(
        None,
        "--ownnetset-v6",
        help="Bird 的 OWNNETSETv6（形如 [prefix+/...]）",
    ),
    bird_conf_path: Path | None = typer.Option(None, "--bird-conf", help="bird.conf 输出路径"),
    bird_peers_dir: Path | None = typer.Option(None, "--bird-peers-dir", help="Bird peers 目录"),
    bird_babel_conf_path: Path | None = typer.Option(None, "--bird-babel-conf", help="babel.conf 输出路径"),
    bird_roa_v6_conf_path: Path | None = typer.Option(None, "--bird-roa-v6-conf", help="roa_dn42_v6.conf 路径"),
    networkd_dir: Path | None = typer.Option(None, "--networkd-dir", help="systemd-networkd 配置目录"),
    nm_system_connections_dir: Path | None = typer.Option(
        None,
        "--nm-system-connections-dir",
        help="NetworkManager system-connections 目录",
    ),
    dummy_backend: str | None = typer.Option(None, "--dummy-backend", help="dummy 网卡后端 (networkd 或 nm)"),
    do_genconf: bool = typer.Option(
        False,
        "--genconf/--no-genconf",
        help="init 完成后是否立即生成 Bird/Babel/ROA 等配置文件 (默认 no-genconf)",
    ),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        existing = appctx.load_config_optional()
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    node_id = existing.node_id if existing else str(uuid.uuid4())

    if own_asn is None:
        own_asn = existing.own_asn if existing else typer.prompt("OWNAS", type=int)
    assert own_asn is not None

    if ownnet_v6 is None:
        ownnet_v6 = existing.ownnet_v6 if existing else typer.prompt("OWNNETv6", default="fddf:8aef:1053::/48")
    assert ownnet_v6 is not None

    if ownnetset_v6 is None:
        ownnetset_v6 = existing.ownnetset_v6 if existing else typer.prompt("OWNNETSETv6", default=f"[{ownnet_v6}+]")
    assert ownnetset_v6 is not None

    if own_ipv6 is None:
        own_ipv6 = existing.own_ipv6 if existing else typer.prompt("OWNIPv6")
    assert own_ipv6 is not None
    if len(own_ipv6) <= 4 and all(c in "0123456789abcdefABCDEF" for c in own_ipv6):
        own_ipv6 = f"fddf:8aef:1053::{own_ipv6.lower()}"

    router_id = existing.router_id if existing else typer.prompt("ROUTERID", default=_default_router_id())

    # Path precedence: CLI option > existing config > default.
    if bird_conf_path is None:
        bird_conf_path = Path(existing.bird_conf_path) if existing else DEFAULT_BIRD_CONF_PATH
    if bird_peers_dir is None:
        bird_peers_dir = Path(existing.bird_peers_dir) if existing else DEFAULT_BIRD_PEERS_DIR
    if bird_babel_conf_path is None:
        bird_babel_conf_path = Path(existing.bird_babel_conf_path) if existing else DEFAULT_BIRD_BABEL_CONF_PATH
    if bird_roa_v6_conf_path is None:
        bird_roa_v6_conf_path = Path(existing.bird_roa_v6_conf_path) if existing else DEFAULT_BIRD_ROA_V6_CONF_PATH
    if networkd_dir is None:
        networkd_dir = Path(existing.networkd_dir) if existing else DEFAULT_NETWORKD_DIR
    if nm_system_connections_dir is None:
        nm_system_connections_dir = (
            Path(existing.nm_system_connections_dir) if existing else DEFAULT_NM_SYSTEM_CONNECTIONS_DIR
        )
    if dummy_backend is None:
        dummy_backend = existing.dummy_backend if existing else typer.prompt("dummy 网卡后端", default="networkd")

    try:
        _cli_validate(validate_asn, own_asn)
        _cli_validate(validate_ipv6_address, own_ipv6, field_name="OWNIPv6")
        _cli_validate(validate_ipv6_network, ownnet_v6, field_name="OWNNETv6")
        _cli_validate(validate_ownnetset_v6, ownnetset_v6)
        _cli_validate(validate_router_id, router_id)
        dummy_backend = _cli_validate(validate_net_backend, dummy_backend)
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    overwrite_bird = False
    overwrite_babel = False
    if do_genconf:
        overwrite_bird = _confirm_overwrite_if_exists(bird_conf_path)
        overwrite_babel = _confirm_overwrite_if_exists(bird_babel_conf_path)

    try:
        init_res = init_node(
            config_path=appctx.config_path,
            db_path=appctx.db_path,
            node_id=node_id,
            own_asn=own_asn,
            router_id=router_id,
            own_ipv6=own_ipv6,
            ownnet_v6=ownnet_v6,
            ownnetset_v6=ownnetset_v6,
            bird_conf_path=bird_conf_path,
            bird_peers_dir=bird_peers_dir,
            bird_babel_conf_path=bird_babel_conf_path,
            bird_roa_v6_conf_path=bird_roa_v6_conf_path,
            networkd_dir=networkd_dir,
            nm_system_connections_dir=nm_system_connections_dir,
            dummy_backend=dummy_backend,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    gen_res = None
    if do_genconf:
        try:
            gen_res = genconf(
                config=init_res.config,
                db_path=appctx.db_path,
                overwrite_bird_conf=overwrite_bird,
                overwrite_babel_conf=overwrite_babel,
            )
        except Dn42CtlError as exc:
            typer.echo(f"错误: {exc}")
            raise typer.Exit(1) from exc

    typer.echo("初始化完成")
    typer.echo(f"node_id: {init_res.config.node_id}")
    typer.echo(f"Config: {init_res.config_path}")
    typer.echo(f"DB: {init_res.db_path}")
    _print_dummy_result(init_res.dummy)

    if gen_res is not None:
        typer.echo(f"Bird: {gen_res.bird_conf_path}")
        typer.echo(f"Babel: {gen_res.bird_babel_conf_path}")
        typer.echo(f"ROA v6: {gen_res.bird_roa_v6_conf_path}")

        if gen_res.warnings:
            typer.echo("\n警告:")
            for w in gen_res.warnings:
                typer.echo(f"- {w}")


@app.command("genconf")
def cmd_genconf(
    ctx: typer.Context,
    all_peers: bool = typer.Option(False, "--all", help="同时重新生成所有 peers 的 Bird 和 WireGuard 配置"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    bird_conf_path = Path(config.bird_conf_path)
    bird_babel_conf_path = Path(config.bird_babel_conf_path)

    overwrite_bird = _confirm_overwrite_if_exists(bird_conf_path)
    overwrite_babel = _confirm_overwrite_if_exists(bird_babel_conf_path)

    try:
        res = genconf(
            config=config,
            db_path=appctx.db_path,
            overwrite_bird_conf=overwrite_bird,
            overwrite_babel_conf=overwrite_babel,
            regenerate_peers=all_peers,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("genconf 完成")
    typer.echo(f"Bird: {res.bird_conf_path}")
    typer.echo(f"Babel: {res.bird_babel_conf_path}")
    typer.echo(f"ROA v6: {res.bird_roa_v6_conf_path}")
    if res.generated_peer_files:
        typer.echo(f"Peers: 已生成 {len(res.generated_peer_files)} 个文件")
    _print_dummy_result(res.dummy)

    if res.warnings:
        typer.echo("\n警告:")
        for w in res.warnings:
            typer.echo(f"- {w}")


@app.command("scan")
def cmd_scan(ctx: typer.Context) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    discovery = discover_bird_paths(
        candidate_bird_conf_paths=[
            Path(config.bird_conf_path),
            Path("/etc/bird/bird.conf"),
            Path("/etc/bird.conf"),
        ]
    )

    updated_config = config
    updated = False
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

    updated_msg: str | None = None
    if updated:
        updated_msg = "已根据 bird.conf 自动更新 config.toml 的 [paths]"
        try:
            save_config(appctx.config_path, updated_config)
        except OSError as exc:
            updated_msg = f"无法回写 config.toml（但将继续使用识别到的路径进行 scan）: {exc}"

    try:
        res = scan_local_configs(config=updated_config, db_path=appctx.db_path)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("scan 完成")
    typer.echo(f"inserted: {len(res.inserted)}  conflicts: {len(res.conflicts)}  skipped: {len(res.skipped)}")

    if res.inserted:
        typer.echo("\n已导入:")
        for x in res.inserted:
            typer.echo(f"- [{x.kind}] {x.key} ifname={x.ifname} backend={x.net_backend}")
    if res.conflicts:
        typer.echo("\n冲突(已跳过):")
        for x in res.conflicts:
            typer.echo(f"- [{x.kind}] {x.key} ifname={x.ifname} backend={x.net_backend}")
    if res.skipped:
        typer.echo("\n跳过:")
        for s in res.skipped:
            typer.echo(f"- {s}")
    warnings_to_print: list[str] = []
    if updated_msg:
        warnings_to_print.append(updated_msg)
    warnings_to_print.extend(discovery.warnings)
    warnings_to_print.extend(res.warnings)
    if warnings_to_print:
        typer.echo("\n警告:")
        for w in warnings_to_print:
            typer.echo(f"- {w}")


bgp_app = typer.Typer()
peer_app = typer.Typer(invoke_without_command=True)


@peer_app.callback(invoke_without_command=True)
def cmd_bgp_peer(
    ctx: typer.Context,
    peer_asn: int | None = typer.Option(None, "--asn", help="Peer ASN"),
    peer_public_key: str | None = typer.Option(None, "--pubkey", help="Peer WireGuard 公钥"),
    endpoint: str | None = typer.Option(None, "--endpoint", help="Peer Endpoint (IP:Port，可留空)"),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则按 ASN 规则推导)",
    ),
    allowed_ips_str: str | None = typer.Option(
        None,
        "--allowed-ips",
        help="WireGuard AllowedIPs (逗号分隔的 IPv6 CIDR，如 fd00::/8,fe80::/64；留空则使用默认值)",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    if peer_asn is None:
        peer_asn = typer.prompt("Peer ASN", type=int)

    parsed_allowed_ips: list[str] | None = None
    if allowed_ips_str is not None:
        try:
            parsed_allowed_ips = _cli_validate(validate_allowed_ips, allowed_ips_str)
        except typer.BadParameter as exc:
            typer.echo(f"输入错误: {exc}")
            raise typer.Exit(2) from exc

    (
        prepared_private_key,
        prepared_public_key,
        prepared_local_lla,
        peer_public_key,
        endpoint,
        peer_lla,
    ) = _prepare_peer_info(peer_public_key, endpoint, peer_lla, allow_empty_endpoint=True)

    assert peer_asn is not None

    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None

    try:
        res = create_bgp_peer(
            config=config,
            db_path=appctx.db_path,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend="networkd",
            listen_port=listen_port,
            wg_private_key=prepared_private_key,
            wg_public_key=prepared_public_key,
            local_lla=prepared_local_lla,
            allowed_ips=parsed_allowed_ips,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("BGP peer 创建完成")
    typer.echo(f"ifname: {res.ifname}")
    typer.echo(f"ListenPort: {res.listen_port}")
    typer.echo(f"本端 WG 公钥: {res.wg_public_key}")
    typer.echo(f"本端 LLA: {res.local_lla}")
    for p in res.generated_files:
        typer.echo(f"写入: {p}")


@peer_app.command("modify")
def cmd_bgp_peer_modify(
    ctx: typer.Context,
    peer_asn: int = typer.Argument(..., help="Peer ASN"),
    peer_public_key: str | None = typer.Option(None, "--pubkey", help="Peer WireGuard 公钥"),
    endpoint: str | None = typer.Option(None, "--endpoint", help="Peer Endpoint (IP:Port)"),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则保持不变)",
    ),
    allowed_ips_str: str | None = typer.Option(
        None,
        "--allowed-ips",
        help="WireGuard AllowedIPs (逗号分隔的 IPv6 CIDR；留空则保持不变)",
    ),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    db = _open_db_or_exit(appctx)
    row = db.get_bgp_peer(config.node_id, peer_asn)
    if row is None:
        typer.echo("错误: 该 peer 不存在")
        raise typer.Exit(2)

    if peer_public_key is None:
        peer_public_key = typer.prompt("Peer 公钥", default=str(row["peer_public_key"] or ""))
    if endpoint is None:
        endpoint = typer.prompt("Peer Endpoint", default=str(row["endpoint"] or ""))
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA", default=str(row["peer_lla"] or ""))

    current_allowed_ips = parse_allowed_ips_json(row["allowed_ips_json"])
    if allowed_ips_str is None:
        allowed_ips_str = typer.prompt("AllowedIPs (逗号分隔)", default=",".join(current_allowed_ips))

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None

    parsed_allowed_ips: list[str] | None = None
    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        if peer_lla:
            peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
        parsed_allowed_ips = _cli_validate(validate_allowed_ips, allowed_ips_str)
    except typer.BadParameter as exc:
        typer.echo(f"\u8f93\u5165\u9519\u8bef: {exc}")
        raise typer.Exit(2) from exc

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None

    try:
        res = modify_bgp_peer(
            config=config,
            db_path=appctx.db_path,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend="networkd",
            listen_port=listen_port,
            allowed_ips=parsed_allowed_ips,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("BGP peer 已重新生成")
    for p in res.generated_files:
        typer.echo(f"写入: {p}")


bgp_app.add_typer(peer_app, name="peer")
app.add_typer(bgp_app, name="bgp")


@peer_app.command("del")
def cmd_bgp_peer_del(
    ctx: typer.Context,
    peer_asn: int = typer.Argument(..., help="Peer ASN"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    if not typer.confirm(f"确认删除 BGP peer AS{peer_asn}（DB + 配置文件）？", default=False):
        typer.echo("已取消")
        return

    try:
        res = delete_bgp_peer(config=config, db_path=appctx.db_path, peer_asn=peer_asn)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("删除完成")
    for p in res.deleted_files:
        typer.echo(f"删除: {p}")
    for p in res.missing_files:
        typer.echo(f"缺失: {p}")


ibgp_app = typer.Typer()
ibgp_peer_app = typer.Typer(invoke_without_command=True)


@ibgp_peer_app.callback(invoke_without_command=True)
def cmd_ibgp_peer(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name", help="Peer 名称 (用于文件名/接口名)"),
    peer_ip: str | None = typer.Option(None, "--peer-ip", help="对端网内 IPv6 地址"),
    no_wg: bool = typer.Option(False, "--no-wg", help="跳过 WireGuard 隧道创建"),
    peer_public_key: str | None = typer.Option(None, "--pubkey", help="Peer WireGuard 公钥"),
    endpoint: str | None = typer.Option(None, "--endpoint", help="Peer Endpoint (IP:Port，可留空)"),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    babel_rxcost: int | None = typer.Option(None, "--rxcost", help="Babel rxcost (0-65535)"),
    babel_type: str | None = typer.Option(
        None, "--type", help="Babel interface type (wired/wireless/tunnel，默认 tunnel)"
    ),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则自动选择未占用端口)",
    ),
    allowed_ips_str: str | None = typer.Option(
        None,
        "--allowed-ips",
        help="WireGuard AllowedIPs (逗号分隔的 IPv6 CIDR；留空则使用默认值)",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    if name is None:
        name = typer.prompt("iBGP peer 名称")
    if peer_ip is None:
        peer_ip = typer.prompt("对端网内 IPv6 地址")

    assert name is not None
    assert peer_ip is not None

    try:
        peer_ip = _cli_validate(validate_ipv6_address, peer_ip, field_name="对端网内 IPv6")
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    parsed_allowed_ips: list[str] | None = None
    if allowed_ips_str is not None:
        try:
            parsed_allowed_ips = _cli_validate(validate_allowed_ips, allowed_ips_str)
        except typer.BadParameter as exc:
            typer.echo(f"输入错误: {exc}")
            raise typer.Exit(2) from exc

    if no_wg:
        assert peer_ip is not None
        try:
            res = create_ibgp_peer(
                config=config,
                db_path=appctx.db_path,
                name=name,
                peer_ip=peer_ip,
                has_wg=False,
                allowed_ips=parsed_allowed_ips,
            )
        except Dn42CtlError as exc:
            typer.echo(f"错误: {exc}")
            raise typer.Exit(1) from exc

        typer.echo("iBGP peer 创建完成 (无 WireGuard)")
        for p in res.generated_files:
            typer.echo(f"写入: {p}")
        return

    if babel_rxcost is None:
        babel_rxcost = typer.prompt("Babel rxcost", type=int)
    if babel_type is None:
        babel_type = typer.prompt("Babel type (wired/wireless/tunnel)", default="tunnel")

    (
        prepared_private_key,
        prepared_public_key,
        prepared_local_lla,
        peer_public_key,
        endpoint,
        peer_lla,
    ) = _prepare_peer_info(peer_public_key, endpoint, peer_lla, allow_empty_endpoint=True)

    assert babel_rxcost is not None
    assert babel_type is not None

    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
        babel_rxcost = _cli_validate(validate_rxcost, babel_rxcost)
        babel_type = _cli_validate(validate_babel_type, babel_type)
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    assert peer_ip is not None
    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert babel_rxcost is not None
    assert babel_type is not None

    try:
        res = create_ibgp_peer(
            config=config,
            db_path=appctx.db_path,
            name=name,
            peer_ip=peer_ip,
            has_wg=True,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend="networkd",
            babel_rxcost=babel_rxcost,
            babel_type=babel_type,
            listen_port=listen_port,
            wg_private_key=prepared_private_key,
            wg_public_key=prepared_public_key,
            local_lla=prepared_local_lla,
            allowed_ips=parsed_allowed_ips,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("iBGP peer 创建完成")
    typer.echo(f"ifname: {res.ifname}")
    typer.echo(f"ListenPort: {res.listen_port}")
    typer.echo(f"本端 WG 公钥: {res.wg_public_key}")
    typer.echo(f"本端 LLA: {res.local_lla}")
    for p in res.generated_files:
        typer.echo(f"写入: {p}")


@ibgp_peer_app.command("modify")
def cmd_ibgp_peer_modify(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="iBGP peer name"),
    peer_public_key: str | None = typer.Option(None, "--pubkey", help="Peer WireGuard 公钥"),
    endpoint: str | None = typer.Option(None, "--endpoint", help="Peer Endpoint (IP:Port)"),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    peer_ip: str | None = typer.Option(None, "--peer-ip", help="对端网内 IPv6 地址"),
    babel_rxcost: int | None = typer.Option(None, "--rxcost", help="Babel rxcost (0-65535)"),
    babel_type: str | None = typer.Option(None, "--type", help="Babel interface type (wired/wireless/tunnel)"),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则保持不变)",
    ),
    allowed_ips_str: str | None = typer.Option(
        None,
        "--allowed-ips",
        help="WireGuard AllowedIPs (逗号分隔的 IPv6 CIDR；留空则保持不变)",
    ),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    db = _open_db_or_exit(appctx)
    try:
        peer_name = sanitize_name(name)
    except Dn42CtlError as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    row = db.get_ibgp_peer(config.node_id, peer_name)
    if row is None:
        typer.echo("错误: 该 peer 不存在")
        raise typer.Exit(2)

    if not bool(row["has_wg"]):
        typer.echo("错误: 该 iBGP peer 未启用 WireGuard，不支持修改 WG 相关参数")
        raise typer.Exit(2)

    if peer_public_key is None:
        peer_public_key = typer.prompt("Peer 公钥", default=str(row["peer_public_key"] or ""))
    if endpoint is None:
        endpoint = typer.prompt("Peer Endpoint", default=str(row["endpoint"] or ""))
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA", default=str(row["peer_lla"] or ""))
    if peer_ip is None:
        peer_ip = typer.prompt("对端网内 IPv6", default=str(row["peer_ip"] or ""))
    if babel_rxcost is None:
        babel_rxcost = typer.prompt("Babel rxcost", type=int, default=int(row["babel_rxcost"]))
    if babel_type is None:
        babel_type = typer.prompt("Babel type (wired/wireless/tunnel)", default=str(row["babel_type"] or "tunnel"))

    current_allowed_ips = parse_allowed_ips_json(row["allowed_ips_json"])
    if allowed_ips_str is None:
        allowed_ips_str = typer.prompt("AllowedIPs (逗号分隔)", default=",".join(current_allowed_ips))

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert peer_ip is not None
    assert babel_rxcost is not None
    assert babel_type is not None

    parsed_allowed_ips: list[str] | None = None
    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        if peer_lla:
            peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
        if peer_ip:
            peer_ip = _cli_validate(validate_ipv6_address, peer_ip, field_name="对端网内 IPv6")
        babel_rxcost = _cli_validate(validate_rxcost, babel_rxcost)
        babel_type = _cli_validate(validate_babel_type, babel_type)
        parsed_allowed_ips = _cli_validate(validate_allowed_ips, allowed_ips_str)
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert peer_ip is not None
    assert babel_rxcost is not None
    assert babel_type is not None

    try:
        res = modify_ibgp_peer(
            config=config,
            db_path=appctx.db_path,
            name=peer_name,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            peer_ip=peer_ip,
            net_backend="networkd",
            babel_rxcost=babel_rxcost,
            babel_type=babel_type,
            listen_port=listen_port,
            allowed_ips=parsed_allowed_ips,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("iBGP peer 已重新生成")
    for p in res.generated_files:
        typer.echo(f"写入: {p}")


ibgp_app.add_typer(ibgp_peer_app, name="peer")
app.add_typer(ibgp_app, name="ibgp")


@ibgp_peer_app.command("del")
def cmd_ibgp_peer_del(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="iBGP peer name"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    if not typer.confirm(
        f"确认删除 iBGP peer '{name}'（DB + 配置文件）？",
        default=False,
    ):
        typer.echo("已取消")
        return

    try:
        res = delete_ibgp_peer(config=config, db_path=appctx.db_path, name=name)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("删除完成")
    for p in res.deleted_files:
        typer.echo(f"删除: {p}")
    for p in res.missing_files:
        typer.echo(f"缺失: {p}")
    for p in res.regenerated_files:
        typer.echo(f"重生成: {p}")


show_app = typer.Typer(invoke_without_command=True)


@show_app.callback(invoke_without_command=True)
def cmd_show_default(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(cmd_show_all, ctx=ctx, as_json=as_json)


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _print_file_statuses(files: list[dict[str, object]]) -> None:
    for f in files:
        path = str(f.get("path") or "")
        exists = bool(f.get("exists"))
        mark = "OK" if exists else "MISSING"
        typer.echo(f"  - {mark}: {path}")


@show_app.command("wg")
def cmd_show_wg(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    try:
        tunnels = show_wg_tunnels(config=config, db_path=appctx.db_path, include_live=True)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    if as_json:
        _print_json([asdict(x) for x in tunnels])
        return

    _print_wg_tunnels(tunnels)


@show_app.command("bgp")
def cmd_show_bgp(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    try:
        peers = show_bgp_peers(config=config, db_path=appctx.db_path, include_live=True)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    if as_json:
        _print_json([asdict(x) for x in peers])
        return

    _print_bgp_peers(peers)


@show_app.command("ibgp")
def cmd_show_ibgp(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    try:
        peers = show_ibgp_peers(config=config, db_path=appctx.db_path, include_live=True)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    if as_json:
        _print_json([asdict(x) for x in peers])
        return

    _print_ibgp_peers(peers)


@show_app.command("all")
def cmd_show_all(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    try:
        tunnels = show_wg_tunnels(config=config, db_path=appctx.db_path, include_live=True)
        bgp = show_bgp_peers(config=config, db_path=appctx.db_path, include_live=True)
        ibgp = show_ibgp_peers(config=config, db_path=appctx.db_path, include_live=True)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    payload: dict[str, object] = {
        "node_id": config.node_id,
        "wg": [asdict(x) for x in tunnels],
        "bgp": [asdict(x) for x in bgp],
        "ibgp": [asdict(x) for x in ibgp],
    }
    if as_json:
        _print_json(payload)
        return

    typer.echo(f"node_id: {config.node_id}")

    typer.echo("\n== wg ==")
    _print_wg_tunnels(tunnels)

    typer.echo("\n== bgp ==")
    _print_bgp_peers(bgp)

    typer.echo("\n== ibgp ==")
    _print_ibgp_peers(ibgp)


app.add_typer(show_app, name="show")


@app.command("serve")
def cmd_serve(
    ctx: typer.Context,
    host: str = typer.Option("::1", "--host", help="绑定地址 (默认 ::1, IPv6 loopback)"),
    port: int = typer.Option(4242, "--port", help="监听端口 (默认 4242)"),
    token: str = typer.Option(..., "--token", envvar="DN42CTL_API_TOKEN", help="Admin Bearer Token (必须提供)"),
    cors_origin: str = typer.Option(
        "",
        "--cors-origin",
        envvar="DN42CTL_CORS_ORIGINS",
        help="允许的 CORS origin (逗号分隔，如 https://admin.example.com,https://peer.example.com)",
    ),
    no_self_register: bool = typer.Option(
        False, "--no-self-register", help="跳过 self node 自动注册 (测试 / 不希望中心机自管时使用)"
    ),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    from dn42ctl.api import app as api_app
    from dn42ctl.api import configure

    configure(config=config, db_path=appctx.db_path, token=token)

    origins = [o.strip() for o in cors_origin.split(",") if o.strip()] if cors_origin else []
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        api_app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    if host not in ("::1", "127.0.0.1", "localhost"):
        typer.echo(
            f"警告: --host={host} 非 loopback 地址。dn42ctl 不处理 TLS,推荐仅监听 loopback 并由 nginx 反代。",
            err=True,
        )

    if not no_self_register:
        from dn42ctl.paths import NODE_CONFIG_PATH, SELF_NODE_ID_PATH
        from dn42ctl.serve_bootstrap import run_self_registration

        try:
            result = run_self_registration(
                db_path=appctx.db_path,
                self_node_id_path=SELF_NODE_ID_PATH,
                node_toml_path=NODE_CONFIG_PATH,
                server_url=f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}",
            )
        except (PermissionError, OSError) as exc:
            typer.echo(f"警告: self node 自动注册失败: {exc}", err=True)
        else:
            note = []
            if result.created_node_id:
                note.append("生成新 self_node_id")
            if result.rotated_token:
                note.append("签发新 self token")
            if note:
                typer.echo(f"self node 自动注册: {result.node_id} ({'; '.join(note)})")
                typer.echo(f"  node.toml -> {result.node_toml_path}")

    import uvicorn

    uvicorn.run(api_app, host=host, port=port)


# --- node management (admin subcommands) ---

node_app = typer.Typer(help="多节点中心化同步: admin 与节点同步命令")


def _print_managed_node(node) -> None:
    flag = " [self]" if node.is_self else ""
    has_token = "yes" if node.api_token_hash else "no"
    typer.echo(f"node_id={node.node_id}{flag} name={node.name} enabled={node.enabled} token={has_token}")
    typer.echo(f"  write_policy: {json.dumps(node.write_policy, ensure_ascii=False)}")
    typer.echo(f"  last_seen_at: {node.last_seen_at or '-'}")
    typer.echo(f"  created_at:   {node.created_at}")
    typer.echo(f"  updated_at:   {node.updated_at}")


@node_app.command("add")
def cmd_node_add(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="UUIDv4 node id"),
    name: str = typer.Option(..., "--name", help="节点显示名"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import add_node

    try:
        node = add_node(db_path=appctx.db_path, node_id=node_id, name=name)
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    _print_managed_node(node)


@node_app.command("list")
def cmd_node_list(ctx: typer.Context) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import list_nodes

    try:
        nodes = list_nodes(db_path=appctx.db_path)
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    if not nodes:
        typer.echo("(没有已注册的 managed_nodes)")
        return
    for n in nodes:
        flag = "[self] " if n.is_self else "       "
        token = "T" if n.api_token_hash else "-"
        typer.echo(f"{flag}{n.node_id}  name={n.name}  enabled={int(n.enabled)}  token={token}")


@node_app.command("show")
def cmd_node_show(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import get_node

    try:
        node = get_node(db_path=appctx.db_path, node_id=node_id)
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    _print_managed_node(node)


@node_app.command("remove")
def cmd_node_remove(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    force: bool = typer.Option(False, "--force", help="允许删除 self 节点"),
    node_config_path: Path = typer.Option(
        None, "--node-config-path", help="覆盖 self node.toml 路径 (force-remove self 节点时被清理)"
    ),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import remove_node

    try:
        node = remove_node(
            db_path=appctx.db_path,
            node_id=node_id,
            force=force,
            self_node_toml_path=node_config_path,
        )
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"已删除: {node.node_id} ({node.name})")
    if node.is_self:
        typer.echo("self 节点的 node.toml 已清理;下次 dn42ctl serve 启动会重新注册")


token_app = typer.Typer(help="节点 token 管理")


@token_app.command("rotate")
def cmd_node_token_rotate(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    node_config_path: Path = typer.Option(
        None, "--node-config-path", help="覆盖 self node.toml 路径 (仅 self 节点轮换 token 时同步)"
    ),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import rotate_token

    try:
        rotated = rotate_token(
            db_path=appctx.db_path,
            node_id=node_id,
            self_node_toml_path=node_config_path,
        )
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"node_id: {rotated.node_id}")
    typer.echo(f"token (明文,仅显示一次): {rotated.plaintext}")
    if rotated.self_node_toml_updated:
        typer.echo(f"已同步更新 self node.toml: {rotated.self_node_toml_path}")


node_app.add_typer(token_app, name="token")


policy_app = typer.Typer(help="节点写策略 (write_policy) 管理")


@policy_app.command("set")
def cmd_node_policy_set(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    peer_add: str = typer.Option(None, "--peer-add", help="review | auto_accept"),
    peer_modify: str = typer.Option(None, "--peer-modify", help="review (固定)"),
    peer_delete: str = typer.Option(None, "--peer-delete", help="review (固定)"),
    report: str = typer.Option(None, "--report", help="review | auto"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import set_policy

    if peer_add is None and peer_modify is None and peer_delete is None and report is None:
        raise typer.BadParameter("至少指定一个 --peer-add / --peer-modify / --peer-delete / --report")
    try:
        node = set_policy(
            db_path=appctx.db_path,
            node_id=node_id,
            peer_add=peer_add,
            peer_modify=peer_modify,
            peer_delete=peer_delete,
            report=report,
        )
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    _print_managed_node(node)


node_app.add_typer(policy_app, name="policy")


app.add_typer(node_app, name="node")


# --- node sync subcommands (spoke side: init / pull / apply / once) ---


def _resolve_node_config_path(appctx: AppContext, override: Path | None) -> Path:
    if override is not None:
        return override
    return Path("/etc/dn42ctl/node.toml")


@node_app.command("init")
def cmd_node_init(
    ctx: typer.Context,
    server: str = typer.Option(..., "--server", help="中心 server URL (含 scheme), 如 https://center.example"),
    node_id: str = typer.Option(..., "--node-id", help="本节点 UUIDv4 (由中心管理员告知)"),
    token: str = typer.Option(..., "--token", help="本节点 token (中心 rotate 后的明文)"),
    node_config_path: Path = typer.Option(None, "--node-config-path", help="覆盖默认 /etc/dn42ctl/node.toml 路径"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.node_config import NodeConfig, save_node_config

    path = _resolve_node_config_path(appctx, node_config_path)
    try:
        save_node_config(path, NodeConfig(server=server, node_id=node_id, token=token))
    except (PermissionError, OSError) as exc:
        typer.echo(f"错误: 无法写入 {path}: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已写入: {path}")


@node_app.command("pull")
def cmd_node_pull(
    ctx: typer.Context,
    node_config_path: Path = typer.Option(None, "--node-config-path"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.node_config import NodeConfigError, load_node_config
    from dn42ctl.services.node_agent import pull

    path = _resolve_node_config_path(appctx, node_config_path)
    try:
        node_cfg = load_node_config(path)
    except NodeConfigError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        result = pull(node_config=node_cfg)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已 pull: revision={result.revision} (cache: {node_cfg.cache_db_path})")


@node_app.command("apply")
def cmd_node_apply(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="不写文件,仅输出 diff"),
    from_server: bool = typer.Option(False, "--from-server", help="apply 前先 pull"),
    node_config_path: Path = typer.Option(None, "--node-config-path"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.node_config import NodeConfigError, load_node_config
    from dn42ctl.services.node_agent import pull
    from dn42ctl.services.node_apply import apply, apply_diff_text, apply_summary

    path = _resolve_node_config_path(appctx, node_config_path)
    try:
        node_cfg = load_node_config(path)
    except NodeConfigError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc

    if from_server:
        try:
            pull(node_config=node_cfg)
        except Dn42CtlError as exc:
            typer.echo(f"错误 (pull): {exc}", err=True)
            raise typer.Exit(1) from exc

    try:
        result = apply(node_config=node_cfg, dry_run=dry_run)
    except Dn42CtlError as exc:
        typer.echo(f"错误 (apply): {exc}", err=True)
        raise typer.Exit(1) from exc
    except (PermissionError, OSError) as exc:
        typer.echo(f"错误: 写文件失败: {exc}", err=True)
        raise typer.Exit(1) from exc

    if dry_run:
        typer.echo(apply_diff_text(result))
    else:
        typer.echo(apply_summary(result))


@node_app.command("once")
def cmd_node_once(
    ctx: typer.Context,
    node_config_path: Path = typer.Option(None, "--node-config-path"),
    no_report: bool = typer.Option(False, "--no-report", help="跳过 apply_result 上报"),
) -> None:
    """pull -> apply -> report (apply_result)"""
    appctx: AppContext = ctx.obj
    from dn42ctl.node_config import NodeConfigError, load_node_config
    from dn42ctl.services.node_agent import pull
    from dn42ctl.services.node_apply import apply, apply_summary
    from dn42ctl.services.node_push import post_report

    path = _resolve_node_config_path(appctx, node_config_path)
    try:
        node_cfg = load_node_config(path)
    except NodeConfigError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        pull_res = pull(node_config=node_cfg)
        typer.echo(f"pulled: revision={pull_res.revision}")
        apply_res = apply(node_config=node_cfg)
        typer.echo(apply_summary(apply_res))
        if not no_report:
            try:
                post_report(
                    node_config=node_cfg,
                    kind="apply_result",
                    payload={
                        "ok": True,
                        "revision": apply_res.revision,
                        "create": sum(1 for d in apply_res.diffs if d.action == "create"),
                        "update": sum(1 for d in apply_res.diffs if d.action == "update"),
                        "unchanged": sum(1 for d in apply_res.diffs if d.action == "unchanged"),
                        "delete": sum(1 for d in apply_res.diffs if d.action == "delete"),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"警告: 上报失败: {exc}", err=True)
    except Dn42CtlError as exc:
        if not no_report:
            # Best-effort error report; failures here are not the user's concern.
            try:
                post_report(node_config=node_cfg, kind="error", payload={"message": str(exc)})
            except Exception as report_exc:  # noqa: BLE001
                typer.echo(f"警告: 错误上报失败: {report_exc}", err=True)
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (PermissionError, OSError) as exc:
        typer.echo(f"错误: 写文件失败: {exc}", err=True)
        raise typer.Exit(1) from exc


@node_app.command("status")
def cmd_node_status(
    ctx: typer.Context,
    node_config_path: Path = typer.Option(None, "--node-config-path"),
) -> None:
    """节点端本地诊断: 读 node.toml + cache + 探活 server。

    打印:
      - node.toml 路径与权限
      - 本地缓存 revision / fetched_at
      - server 可达性 (GET /status) + 中心视角 (last_seen / current_revision / pinned)
    """
    appctx: AppContext = ctx.obj
    from dn42ctl.node_client import NodeClient, NodeClientError
    from dn42ctl.node_config import NodeConfigError, load_node_config
    from dn42ctl.services.node_agent import read_cache

    path = _resolve_node_config_path(appctx, node_config_path)
    typer.echo(f"node.toml: {path}")
    try:
        st = path.stat()
        typer.echo(f"  权限: 0o{st.st_mode & 0o777:o}")
    except FileNotFoundError:
        typer.echo("  错误: 文件不存在 (先运行 dn42ctl node init)")
        raise typer.Exit(1) from None

    try:
        node_cfg = load_node_config(path)
    except NodeConfigError as exc:
        typer.echo(f"  错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"  server: {node_cfg.server}")
    typer.echo(f"  node_id: {node_cfg.node_id}")
    typer.echo(f"  token: {'<set>' if node_cfg.token else '<missing>'}")

    typer.echo(f"cache: {node_cfg.cache_db_path}")
    cached = read_cache(node_config=node_cfg)
    if cached is None:
        typer.echo("  (没有缓存; 运行 dn42ctl node pull)")
    else:
        typer.echo(f"  revision: {cached.revision}")
        typer.echo(f"  fetched_at: {cached.fetched_at}")

    typer.echo(f"server: {node_cfg.server}")
    client = NodeClient(
        server=node_cfg.server,
        node_id=node_cfg.node_id,
        token=node_cfg.token,
        timeout=3.0,
    )
    try:
        remote = client.fetch_status()
    except NodeClientError as exc:
        typer.echo(f"  不可达: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"  last_seen_at: {remote.get('last_seen_at') or '-'}")
    typer.echo(f"  current_revision: {remote.get('current_revision') or '-'}")
    typer.echo(f"  pinned_revision: {remote.get('pinned_revision') or '(none)'}")
    if cached is not None and remote.get("current_revision"):
        diff = "同步" if cached.revision == remote["current_revision"] else "落后/超前 (revision 不一致)"
        typer.echo(f"  本地缓存 vs 中心: {diff}")


# --- stage 3: node push/scan/report (spoke) + admin proposals/reports listing ---


@node_app.command("push")
def cmd_node_push(
    ctx: typer.Context,
    json_path: Path = typer.Option(..., "--json", help="proposals 列表 JSON 文件: [{kind,payload}, ...]"),
    source: str = typer.Option("push", "--source", help="push | scan"),
    node_config_path: Path = typer.Option(None, "--node-config-path"),
) -> None:
    """Push 一组结构化 proposals 到 server。

    JSON 格式: [{"kind": "peer_add", "payload": {...}}, ...]
    kind ∈ {peer_add, peer_modify, peer_delete}.
    """
    appctx: AppContext = ctx.obj
    from dn42ctl.node_config import NodeConfigError, load_node_config
    from dn42ctl.services.node_push import post_proposal

    path = _resolve_node_config_path(appctx, node_config_path)
    try:
        node_cfg = load_node_config(path)
    except NodeConfigError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        items = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"错误: 无法读取 JSON: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(items, list):
        typer.echo("错误: JSON 顶层必须是数组", err=True)
        raise typer.Exit(1)
    submitted = 0
    for item in items:
        if not isinstance(item, dict) or "kind" not in item or "payload" not in item:
            typer.echo(f"错误: 跳过非法 item: {item}", err=True)
            continue
        try:
            res = post_proposal(node_config=node_cfg, kind=item["kind"], payload=item["payload"], source=source)
        except Dn42CtlError as exc:
            typer.echo(f"错误: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"提案 #{res['id']} kind={res['kind']} status={res['status']}")
        submitted += 1
    typer.echo(f"共提交 {submitted} 条提案")


@node_app.command("report")
def cmd_node_report(
    ctx: typer.Context,
    kind: str = typer.Option(..., "--kind", help="apply_result | scan_result | live_status | error"),
    json_path: Path = typer.Option(..., "--json", help="payload JSON 文件"),
    node_config_path: Path = typer.Option(None, "--node-config-path"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.node_config import NodeConfigError, load_node_config
    from dn42ctl.services.node_push import post_report

    path = _resolve_node_config_path(appctx, node_config_path)
    try:
        node_cfg = load_node_config(path)
    except NodeConfigError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"错误: 无法读取 JSON: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(payload, dict):
        typer.echo("错误: report payload 必须是对象", err=True)
        raise typer.Exit(1)
    try:
        res = post_report(node_config=node_cfg, kind=kind, payload=payload)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"上报 #{res['id']} kind={res['kind']} at={res['received_at']}")


@node_app.command("proposals")
def cmd_node_proposals(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    status: str = typer.Option(None, "--status", help="过滤: pending | accepted | rejected"),
    limit: int = typer.Option(200, "--limit", help="最大返回数"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import list_proposals

    try:
        rows = list_proposals(db_path=appctx.db_path, node_id=node_id, status=status, limit=limit)
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    if not rows:
        typer.echo("(没有 proposal)")
        return
    for p in rows:
        decided = p.decided_at or "-"
        typer.echo(
            f"#{p.id} kind={p.kind} source={p.source} status={p.status} received={p.received_at} decided={decided}"
        )


@node_app.command("reports")
def cmd_node_reports(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    kind: str = typer.Option(None, "--kind", help="apply_result | scan_result | live_status | error"),
    limit: int = typer.Option(50, "--limit", help="最大返回数"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import list_reports

    try:
        rows = list_reports(db_path=appctx.db_path, node_id=node_id, kind=kind, limit=limit)
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    if not rows:
        typer.echo("(没有 report)")
        return
    for r in rows:
        imp = r.imported_at or "-"
        typer.echo(f"#{r.id} kind={r.kind} received={r.received_at} imported={imp}")


# --- stage 4: admin proposal decisions / report import ---


@node_app.command("accept-proposal")
def cmd_node_accept_proposal(
    ctx: typer.Context,
    proposal_id: int = typer.Argument(..., help="proposal id"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)
    from dn42ctl.services import accept_proposal

    try:
        p = accept_proposal(config=config, db_path=appctx.db_path, proposal_id=proposal_id)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已接受 proposal #{p.id} kind={p.kind} status={p.status} at={p.decided_at}")


@node_app.command("reject-proposal")
def cmd_node_reject_proposal(
    ctx: typer.Context,
    proposal_id: int = typer.Argument(..., help="proposal id"),
    reason: str = typer.Option(..., "--reason", help="拒绝原因 (必填)"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import reject_proposal

    try:
        p = reject_proposal(db_path=appctx.db_path, proposal_id=proposal_id, reason=reason)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已拒绝 proposal #{p.id}: {p.message}")


@node_app.command("import-report")
def cmd_node_import_report(
    ctx: typer.Context,
    report_id: int = typer.Argument(..., help="report id"),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)
    from dn42ctl.services import import_report

    try:
        counts = import_report(config=config, db_path=appctx.db_path, report_id=report_id)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"已导入 report #{report_id}: "
        f"bgp(created={counts['bgp_created']}, skipped={counts['bgp_skipped']}), "
        f"ibgp(created={counts['ibgp_created']}, skipped={counts['ibgp_skipped']})"
    )


# --- stage 5: revisions / rollback ---


@node_app.command("revisions")
def cmd_node_revisions(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import get_pinned, list_revisions

    try:
        rows = list_revisions(db_path=appctx.db_path, node_id=node_id, limit=limit)
        pin = get_pinned(db_path=appctx.db_path, node_id=node_id)
    except Dn42CtlError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DatabaseError as exc:
        typer.echo(_db_open_hint(appctx.db_path), err=True)
        raise typer.Exit(code=1) from exc
    pin_rev = pin.revision if pin else None
    typer.echo(f"pinned: {pin_rev or '(none, following latest)'}")
    if not rows:
        typer.echo("(没有 revision)")
        return
    for r in rows:
        marker = " *" if r.revision == pin_rev else "  "
        typer.echo(f"{marker} #{r.id} revision={r.revision} generated={r.generated_at}")


@node_app.command("rollback")
def cmd_node_rollback(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
    revision: str = typer.Option(..., "--to", help="目标 revision"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import rollback_to

    try:
        rev = rollback_to(db_path=appctx.db_path, node_id=node_id, revision=revision)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已 pin: node={rev.node_id} revision={rev.revision}")


@node_app.command("rollback-clear")
def cmd_node_rollback_clear(
    ctx: typer.Context,
    node_id: str = typer.Argument(..., help="节点 UUID"),
) -> None:
    appctx: AppContext = ctx.obj
    from dn42ctl.services import clear_rollback

    try:
        clear_rollback(db_path=appctx.db_path, node_id=node_id)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已清除 pin: node={node_id} (恢复到最新)")


# --- system install / uninstall ---

system_app = typer.Typer(help="系统组件安装/卸载 (firewalld, nftables, ROA timer)")

_SYSTEM_COMPONENTS = ["firewalld-conf", "nftables-conf", "roa-service"]


def _print_system_result(result: object) -> None:
    from dn42ctl.services.system import SystemInstallResult

    if not isinstance(result, SystemInstallResult):
        return
    for f in result.changed_files:
        typer.echo(f"  变更: {f}")
    for w in result.warnings:
        typer.echo(f"  警告: {w}")


@system_app.command("install")
def cmd_system_install(
    ctx: typer.Context,
    component: str = typer.Argument(..., help=f"组件名: {' | '.join(_SYSTEM_COMPONENTS)}"),
) -> None:
    from dn42ctl.services import (
        install_firewalld_conf,
        install_nftables_conf,
        install_roa_service,
    )

    appctx: AppContext = ctx.obj

    if component not in _SYSTEM_COMPONENTS:
        typer.echo(f"错误: 未知组件 '{component}'，可选: {', '.join(_SYSTEM_COMPONENTS)}")
        raise typer.Exit(2)

    try:
        if component == "firewalld-conf":
            result = install_firewalld_conf()
        elif component == "nftables-conf":
            result = install_nftables_conf()
        elif component == "roa-service":
            config = _require_config_or_exit(appctx)
            result = install_roa_service(config=config)
        else:
            raise typer.Exit(2)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo(f"{component} 安装完成")
    _print_system_result(result)


@system_app.command("uninstall")
def cmd_system_uninstall(
    ctx: typer.Context,
    component: str = typer.Argument(..., help=f"组件名: {' | '.join(_SYSTEM_COMPONENTS)}"),
) -> None:
    from dn42ctl.services import (
        uninstall_firewalld_conf,
        uninstall_nftables_conf,
        uninstall_roa_service,
    )

    if component not in _SYSTEM_COMPONENTS:
        typer.echo(f"错误: 未知组件 '{component}'，可选: {', '.join(_SYSTEM_COMPONENTS)}")
        raise typer.Exit(2)

    try:
        if component == "firewalld-conf":
            result = uninstall_firewalld_conf()
        elif component == "nftables-conf":
            result = uninstall_nftables_conf()
        elif component == "roa-service":
            result = uninstall_roa_service()
        else:
            raise typer.Exit(2)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo(f"{component} 卸载完成")
    _print_system_result(result)


app.add_typer(system_app, name="system")


# --- deploy ---

deploy_app = typer.Typer(help="部署 Web UI / daemon 到系统目录")


def _find_pkg_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _find_web_dir() -> Path:
    web_dir = _find_pkg_root() / "web"
    if not (web_dir / "package.json").exists():
        typer.echo(f"错误: 找不到 web 目录 ({web_dir})")
        raise typer.Exit(1)
    return web_dir


def _restorecon(path: Path) -> None:
    import shutil
    import subprocess

    restorecon = shutil.which("restorecon")
    if restorecon is not None:
        subprocess.run(  # noqa: S603
            [restorecon, "-Rv", str(path)],
            check=False,
        )


@deploy_app.command("web")
def cmd_deploy_web(
    dest: Path = typer.Argument(..., help="部署目标目录 (如 /var/www/dn42ctl)"),
    skip_build: bool = typer.Option(False, "--skip-build", help="跳过构建，直接复制已有 dist/"),
    api_base: str = typer.Option(
        "",
        "--api-base",
        help="设置 VITE_API_BASE 环境变量 (如 https://api.dn42.example.com)，仅在构建时生效",
    ),
) -> None:
    """构建 web UI 并复制到指定目录。"""
    import shutil
    import subprocess

    web_dir = _find_web_dir()
    dist_dir = web_dir / "dist"

    if not skip_build:
        pnpm = shutil.which("pnpm")
        if pnpm is None:
            typer.echo("错误: 未找到 pnpm，请先安装 (npm install -g pnpm)")
            raise typer.Exit(1)

        build_env = {**os.environ}
        if api_base:
            build_env["VITE_API_BASE"] = api_base
            typer.echo(f"VITE_API_BASE={api_base}")

        typer.echo("正在构建 web UI...")
        try:
            subprocess.run(  # noqa: S603
                [pnpm, "install", "--frozen-lockfile"],
                cwd=str(web_dir),
                check=True,
            )
            subprocess.run(  # noqa: S603
                [pnpm, "build"],
                cwd=str(web_dir),
                env=build_env,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            typer.echo(f"错误: 构建失败 (exit {exc.returncode})")
            raise typer.Exit(1) from exc

    if not dist_dir.exists():
        typer.echo(f"错误: dist/ 不存在 ({dist_dir})，请先运行构建")
        raise typer.Exit(1)

    dest.mkdir(parents=True, exist_ok=True)

    for name in ("admin", "peer", "assets"):
        src = dist_dir / name
        if not src.exists():
            continue
        target = dest / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        typer.echo(f"  {src} -> {target}")

    _restorecon(dest)
    typer.echo(f"部署完成: {dest}")


@deploy_app.command("daemon")
def cmd_deploy_daemon(
    dest: Path = typer.Option(
        Path("/usr/local/bin"),
        "--dest",
        help="可执行文件安装目录 (默认 /usr/local/bin)",
    ),
    tool_dir: Path = typer.Option(
        Path("/opt/dn42ctl"),
        "--tool-dir",
        help="uv tool venv 安装目录 (默认 /opt/dn42ctl，需要 systemd 可读)",
    ),
) -> None:
    """安装 dn42ctl 到系统路径，供 systemd 调用。

    venv 放在 --tool-dir 而非 ~/.local/share/uv/tools/，
    避免 systemd ProtectHome=true 导致无法启动。
    """
    import shutil
    import subprocess

    pkg_root = _find_pkg_root()

    uv = shutil.which("uv")
    if uv is None:
        typer.echo("错误: 未找到 uv")
        raise typer.Exit(1)

    dest.mkdir(parents=True, exist_ok=True)
    tool_dir.mkdir(parents=True, exist_ok=True)

    commit_file = pkg_root / "src" / "dn42ctl" / "_build_commit.txt"
    tmp_commit = Path("/tmp/dn42ctl_build_commit.txt")  # noqa: S108
    try:
        commit_hash = subprocess.check_output(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=pkg_root,
            text=True,
        ).strip()
        tmp_commit.write_text(commit_hash, encoding="utf-8")
        shutil.copy2(tmp_commit, commit_file)
        typer.echo(f"注入 commit: {commit_hash[:12]}")
    except (subprocess.CalledProcessError, OSError) as exc:
        typer.echo(f"警告: 无法获取 git commit ({exc})")

    env = {
        **__import__("os").environ,
        "UV_TOOL_BIN_DIR": str(dest),
        "UV_TOOL_DIR": str(tool_dir),
    }

    typer.echo(f"正在安装 dn42ctl 到 {dest} (venv: {tool_dir}) ...")
    try:
        subprocess.run(  # noqa: S603
            [uv, "tool", "install", "--force", "--reinstall", str(pkg_root)],
            env=env,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        typer.echo(f"错误: 安装失败 (exit {exc.returncode})")
        raise typer.Exit(1) from exc
    finally:
        commit_file.unlink(missing_ok=True)
        tmp_commit.unlink(missing_ok=True)

    _restorecon(dest / "dn42ctl")
    typer.echo(f"已安装: {dest / 'dn42ctl'}")


app.add_typer(deploy_app, name="deploy")
