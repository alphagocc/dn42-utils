from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import typer

from dn42ctl.context import AppContext
from dn42ctl.db import Database, DatabaseError
from dn42ctl.config import AppConfig, ConfigError, save_config
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
    discover_bird_paths,
    delete_bgp_peer,
    delete_ibgp_peer,
    genconf,
    init_node,
    modify_bgp_peer,
    modify_ibgp_peer,
    scan_local_configs,
    show_bgp_peers,
    show_ibgp_peers,
    show_wg_tunnels,
)
from dn42ctl.services.core import BgpPeerView, IbgpPeerView, WgTunnelView, sanitize_name
from dn42ctl.validators import (
    ValidationError as _ValidationError,
    validate_asn,
    validate_endpoint,
    validate_ipv6_address,
    validate_ipv6_network,
    validate_ownnetset_v6,
    validate_pubkey,
    validate_router_id,
    validate_rxcost,
    validate_babel_type,
)
from dn42ctl.wg import WireGuardError, generate_random_lla_cidr, generate_wg_keypair


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
            prepared_local_lla = generate_random_lla_cidr()
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
        typer.echo(
            f"[{t.kind}] {ident} ifname={t.ifname} backend={t.net_backend} port={t.listen_port}"
        )
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
        typer.echo(
            f"AS{p.peer_asn} ifname={p.ifname} backend={p.net_backend} port={p.listen_port}"
        )
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
        typer.echo(
            f"{p.name} proto={proto} ifname={p.ifname} [{wg_tag}] backend={p.net_backend} port={p.listen_port}"
        )
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
    ownnet_v6: str | None = typer.Option(
        None, "--ownnet-v6", help="本机 DN42 IPv6 前缀"
    ),
    ownnetset_v6: str | None = typer.Option(
        None,
        "--ownnetset-v6",
        help="Bird 的 OWNNETSETv6（形如 [prefix+/...]）",
    ),
    bird_conf_path: Path | None = typer.Option(
        None, "--bird-conf", help="bird.conf 输出路径"
    ),
    bird_peers_dir: Path | None = typer.Option(
        None, "--bird-peers-dir", help="Bird peers 目录"
    ),
    bird_babel_conf_path: Path | None = typer.Option(
        None, "--bird-babel-conf", help="babel.conf 输出路径"
    ),
    bird_roa_v6_conf_path: Path | None = typer.Option(
        None, "--bird-roa-v6-conf", help="roa_dn42_v6.conf 路径"
    ),
    networkd_dir: Path | None = typer.Option(
        None, "--networkd-dir", help="systemd-networkd 配置目录"
    ),
    nm_system_connections_dir: Path | None = typer.Option(
        None,
        "--nm-system-connections-dir",
        help="NetworkManager system-connections 目录",
    ),
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
        ownnet_v6 = (
            existing.ownnet_v6
            if existing
            else typer.prompt("OWNNETv6", default="fddf:8aef:1053::/48")
        )
    assert ownnet_v6 is not None

    if ownnetset_v6 is None:
        ownnetset_v6 = (
            existing.ownnetset_v6
            if existing
            else typer.prompt("OWNNETSETv6", default=f"[{ownnet_v6}+]")
        )
    assert ownnetset_v6 is not None

    if own_ipv6 is None:
        own_ipv6 = existing.own_ipv6 if existing else typer.prompt("OWNIPv6")
    assert own_ipv6 is not None
    if len(own_ipv6) <= 4 and all(c in "0123456789abcdefABCDEF" for c in own_ipv6):
        own_ipv6 = f"fddf:8aef:1053::{own_ipv6.lower()}"

    router_id = (
        existing.router_id
        if existing
        else typer.prompt("ROUTERID", default=_default_router_id())
    )

    # Path precedence: CLI option > existing config > default.
    if bird_conf_path is None:
        bird_conf_path = (
            Path(existing.bird_conf_path) if existing else DEFAULT_BIRD_CONF_PATH
        )
    if bird_peers_dir is None:
        bird_peers_dir = (
            Path(existing.bird_peers_dir) if existing else DEFAULT_BIRD_PEERS_DIR
        )
    if bird_babel_conf_path is None:
        bird_babel_conf_path = (
            Path(existing.bird_babel_conf_path)
            if existing
            else DEFAULT_BIRD_BABEL_CONF_PATH
        )
    if bird_roa_v6_conf_path is None:
        bird_roa_v6_conf_path = (
            Path(existing.bird_roa_v6_conf_path)
            if existing
            else DEFAULT_BIRD_ROA_V6_CONF_PATH
        )
    if networkd_dir is None:
        networkd_dir = Path(existing.networkd_dir) if existing else DEFAULT_NETWORKD_DIR
    if nm_system_connections_dir is None:
        nm_system_connections_dir = (
            Path(existing.nm_system_connections_dir)
            if existing
            else DEFAULT_NM_SYSTEM_CONNECTIONS_DIR
        )

    try:
        _cli_validate(validate_asn, own_asn)
        _cli_validate(validate_ipv6_address, own_ipv6, field_name="OWNIPv6")
        _cli_validate(validate_ipv6_network, ownnet_v6, field_name="OWNNETv6")
        _cli_validate(validate_ownnetset_v6, ownnetset_v6)
        _cli_validate(validate_router_id, router_id)
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
        typer.echo(
            "ROA systemd timer: "
            + ("enabled" if gen_res.systemd_roa_timer_enabled else "skipped")
        )

        if gen_res.warnings:
            typer.echo("\n警告:")
            for w in gen_res.warnings:
                typer.echo(f"- {w}")


@app.command("genconf")
def cmd_genconf(ctx: typer.Context) -> None:
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
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("genconf 完成")
    typer.echo(f"Bird: {res.bird_conf_path}")
    typer.echo(f"Babel: {res.bird_babel_conf_path}")
    typer.echo(f"ROA v6: {res.bird_roa_v6_conf_path}")
    typer.echo(
        "ROA systemd timer: "
        + ("enabled" if res.systemd_roa_timer_enabled else "skipped")
    )
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
            updated_msg = (
                f"无法回写 config.toml（但将继续使用识别到的路径进行 scan）: {exc}"
            )

    try:
        res = scan_local_configs(config=updated_config, db_path=appctx.db_path)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("scan 完成")
    typer.echo(
        f"inserted: {len(res.inserted)}  conflicts: {len(res.conflicts)}  skipped: {len(res.skipped)}"
    )

    if res.inserted:
        typer.echo("\n已导入:")
        for x in res.inserted:
            typer.echo(
                f"- [{x.kind}] {x.key} ifname={x.ifname} backend={x.net_backend}"
            )
    if res.conflicts:
        typer.echo("\n冲突(已跳过):")
        for x in res.conflicts:
            typer.echo(
                f"- [{x.kind}] {x.key} ifname={x.ifname} backend={x.net_backend}"
            )
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
    peer_public_key: str | None = typer.Option(
        None, "--pubkey", help="Peer WireGuard 公钥"
    ),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="Peer Endpoint (IP:Port，可留空)"
    ),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    net_backend: str | None = typer.Option(
        None, "--net", help="networkd 或 nm (NetworkManager)"
    ),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则按 ASN 规则推导)",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    if peer_asn is None:
        peer_asn = typer.prompt("Peer ASN", type=int)
    if net_backend is None:
        net_backend = typer.prompt("网络后端", default="networkd")

    (
        prepared_private_key,
        prepared_public_key,
        prepared_local_lla,
        peer_public_key,
        endpoint,
        peer_lla,
    ) = _prepare_peer_info(
        peer_public_key, endpoint, peer_lla, allow_empty_endpoint=True
    )

    assert peer_asn is not None
    assert net_backend is not None

    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    try:
        res = create_bgp_peer(
            config=config,
            db_path=appctx.db_path,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend=net_backend,
            listen_port=listen_port,
            wg_private_key=prepared_private_key,
            wg_public_key=prepared_public_key,
            local_lla=prepared_local_lla,
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
    peer_public_key: str | None = typer.Option(
        None, "--pubkey", help="Peer WireGuard 公钥"
    ),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="Peer Endpoint (IP:Port)"
    ),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    net_backend: str | None = typer.Option(None, "--net", help="networkd 或 nm"),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则保持不变)",
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
        peer_public_key = typer.prompt(
            "Peer 公钥", default=str(row["peer_public_key"] or "")
        )
    if endpoint is None:
        endpoint = typer.prompt("Peer Endpoint", default=str(row["endpoint"] or ""))
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA", default=str(row["peer_lla"] or ""))
    if net_backend is None:
        net_backend = typer.prompt(
            "网络后端", default=str(row["net_backend"] or "networkd")
        )

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert net_backend is not None

    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        if peer_lla:
            peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
    except typer.BadParameter as exc:
        typer.echo(f"\u8f93\u5165\u9519\u8bef: {exc}")
        raise typer.Exit(2) from exc

    try:
        res = modify_bgp_peer(
            config=config,
            db_path=appctx.db_path,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend=net_backend,
            listen_port=listen_port,
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

    if not typer.confirm(
        f"确认删除 BGP peer AS{peer_asn}（DB + 配置文件）？", default=False
    ):
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
    name: str | None = typer.Option(
        None, "--name", help="Peer 名称 (用于文件名/接口名)"
    ),
    peer_ip: str | None = typer.Option(
        None, "--peer-ip", help="对端网内 IPv6 地址"
    ),
    no_wg: bool = typer.Option(
        False, "--no-wg", help="跳过 WireGuard 隧道创建"
    ),
    peer_public_key: str | None = typer.Option(
        None, "--pubkey", help="Peer WireGuard 公钥"
    ),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="Peer Endpoint (IP:Port，可留空)"
    ),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    net_backend: str | None = typer.Option(None, "--net", help="networkd 或 nm"),
    babel_rxcost: int | None = typer.Option(
        None, "--rxcost", help="Babel rxcost (0-65535)"
    ),
    babel_type: str | None = typer.Option(
        None, "--type", help="Babel interface type (wired/wireless/tunnel，默认 tunnel)"
    ),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则自动选择未占用端口)",
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

    if no_wg:
        try:
            res = create_ibgp_peer(
                config=config,
                db_path=appctx.db_path,
                name=name,
                peer_ip=peer_ip,
                has_wg=False,
            )
        except Dn42CtlError as exc:
            typer.echo(f"错误: {exc}")
            raise typer.Exit(1) from exc

        typer.echo("iBGP peer 创建完成 (无 WireGuard)")
        for p in res.generated_files:
            typer.echo(f"写入: {p}")
        return

    if net_backend is None:
        net_backend = typer.prompt("网络后端", default="networkd")
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
    ) = _prepare_peer_info(
        peer_public_key, endpoint, peer_lla, allow_empty_endpoint=True
    )

    assert net_backend is not None
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
            net_backend=net_backend,
            babel_rxcost=babel_rxcost,
            babel_type=babel_type,
            listen_port=listen_port,
            wg_private_key=prepared_private_key,
            wg_public_key=prepared_public_key,
            local_lla=prepared_local_lla,
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
    peer_public_key: str | None = typer.Option(
        None, "--pubkey", help="Peer WireGuard 公钥"
    ),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="Peer Endpoint (IP:Port)"
    ),
    peer_lla: str | None = typer.Option(None, "--peer-lla", help="Peer LLA (IPv6)"),
    peer_ip: str | None = typer.Option(None, "--peer-ip", help="对端网内 IPv6 地址"),
    net_backend: str | None = typer.Option(None, "--net", help="networkd 或 nm"),
    babel_rxcost: int | None = typer.Option(
        None, "--rxcost", help="Babel rxcost (0-65535)"
    ),
    babel_type: str | None = typer.Option(
        None, "--type", help="Babel interface type (wired/wireless/tunnel)"
    ),
    listen_port: int | None = typer.Option(
        None,
        "--listen-port",
        help="本端 ListenPort (0 表示不设置；留空则保持不变)",
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
        peer_public_key = typer.prompt(
            "Peer 公钥", default=str(row["peer_public_key"] or "")
        )
    if endpoint is None:
        endpoint = typer.prompt("Peer Endpoint", default=str(row["endpoint"] or ""))
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA", default=str(row["peer_lla"] or ""))
    if peer_ip is None:
        peer_ip = typer.prompt("对端网内 IPv6", default=str(row["peer_ip"] or ""))
    if net_backend is None:
        net_backend = typer.prompt(
            "网络后端", default=str(row["net_backend"] or "networkd")
        )
    if babel_rxcost is None:
        babel_rxcost = typer.prompt("Babel rxcost", type=int, default=int(row["babel_rxcost"]))
    if babel_type is None:
        babel_type = typer.prompt("Babel type (wired/wireless/tunnel)", default=str(row["babel_type"] or "tunnel"))

    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert peer_ip is not None
    assert net_backend is not None
    assert babel_rxcost is not None
    assert babel_type is not None

    try:
        peer_public_key = _cli_validate(validate_pubkey, peer_public_key)
        endpoint = _cli_validate(validate_endpoint, endpoint, allow_empty=True)
        if peer_lla:
            peer_lla = _cli_validate(validate_ipv6_address, peer_lla, field_name="Peer LLA")
        if peer_ip:
            peer_ip = _cli_validate(validate_ipv6_address, peer_ip, field_name="对端网内 IPv6")
        babel_rxcost = _cli_validate(validate_rxcost, babel_rxcost)
        babel_type = _cli_validate(validate_babel_type, babel_type)
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    try:
        res = modify_ibgp_peer(
            config=config,
            db_path=appctx.db_path,
            name=peer_name,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            peer_ip=peer_ip,
            net_backend=net_backend,
            babel_rxcost=babel_rxcost,
            babel_type=babel_type,
            listen_port=listen_port,
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
        tunnels = show_wg_tunnels(
            config=config, db_path=appctx.db_path, include_live=True
        )
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
        peers = show_ibgp_peers(
            config=config, db_path=appctx.db_path, include_live=True
        )
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
        tunnels = show_wg_tunnels(
            config=config, db_path=appctx.db_path, include_live=True
        )
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
    host: str = typer.Option("127.0.0.1", "--host", help="绑定地址 (默认 127.0.0.1)"),
    port: int = typer.Option(4242, "--port", help="监听端口 (默认 4242)"),
    token: str = typer.Option(
        ..., "--token", envvar="DN42CTL_API_TOKEN", help="Bearer Token (必须提供)"
    ),
) -> None:
    appctx: AppContext = ctx.obj
    config = _require_config_or_exit(appctx)

    from dn42ctl.api import app as api_app, configure

    configure(config=config, db_path=appctx.db_path, token=token)

    import uvicorn

    uvicorn.run(api_app, host=host, port=port)
