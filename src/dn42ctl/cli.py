from __future__ import annotations

import ipaddress
import json
import re
import secrets
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import typer

from dn42ctl.context import AppContext
from dn42ctl.db import DatabaseError
from dn42ctl.config import ConfigError, save_config
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
    scan_local_configs,
    show_bgp_peers,
    show_ibgp_peers,
    show_wg_tunnels,
)


app = typer.Typer(add_completion=False)


def _db_open_hint(db_path: Path) -> str:
    return (
        f"无法打开数据库: {db_path}。"
        "若使用默认系统路径，通常需要以 root 运行（sudo），"
        "或使用 --db-path 覆盖到可写位置。"
    )


_WG_PUBKEY_RE = re.compile(r"^[A-Za-z0-9+/]{42,44}={0,2}$")


def _validate_pubkey(value: str) -> str:
    """Validate a WireGuard public key: non-empty, base64 charset, ~44 chars."""
    value = value.strip()
    if not value:
        raise typer.BadParameter("公钥不能为空")
    if not _WG_PUBKEY_RE.match(value):
        raise typer.BadParameter(f"公钥格式不合法 (base64, 需40~44字符): {value!r}")
    return value


def _validate_endpoint(value: str) -> str:
    """Validate endpoint: allow empty, or require host:port with port 1-65535."""
    value = value.strip()
    if not value:
        return value
    # Support IPv6 bracket notation [::1]:port and plain host:port.
    m = re.match(r"^(\[.+\]|[^:]+):(\d+)$", value)
    if not m:
        raise typer.BadParameter(
            f"格式错误: 需要 host:port 或 [IPv6]:port 形式: {value!r}"
        )
    port = int(m.group(2))
    if not (1 <= port <= 65535):
        raise typer.BadParameter(f"Port 超出范围 (1-65535): {port}")
    return value


def _validate_peer_lla(value: str) -> str:
    """Validate peer LLA: non-empty and parseable as an IPv6 address."""
    value = value.strip()
    if not value:
        raise typer.BadParameter("Peer LLA 不能为空")
    # Strip /prefix-length for validation.
    addr_part = value.split("/", 1)[0]
    try:
        ipaddress.IPv6Address(addr_part)
    except ValueError:
        raise typer.BadParameter(f"不是合法的 IPv6 地址: {value!r}")
    return value


def _default_router_id() -> str:
    a = secrets.randbelow(254) + 1
    b = secrets.randbelow(254) + 1
    return f"169.254.{a}.{b}"


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

    if own_ipv6 is None:
        own_ipv6 = existing.own_ipv6 if existing else typer.prompt("OWNIPv6")
    assert own_ipv6 is not None
    if len(own_ipv6) == 4 and all(c in "0123456789abcdefABCDEF" for c in own_ipv6):
        own_ipv6 = f"fddf:8aef:1053::{own_ipv6.lower()}"

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

    overwrite_bird = False
    overwrite_babel = False
    if do_genconf:
        overwrite_bird = True
        if bird_conf_path.exists():
            overwrite_bird = typer.confirm(
                f"{bird_conf_path} 已存在，覆盖？", default=False
            )

        overwrite_babel = True
        if bird_babel_conf_path.exists():
            overwrite_babel = typer.confirm(
                f"{bird_babel_conf_path} 已存在，覆盖？", default=False
            )

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
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    bird_conf_path = Path(config.bird_conf_path)
    bird_babel_conf_path = Path(config.bird_babel_conf_path)

    overwrite_bird = True
    if bird_conf_path.exists():
        overwrite_bird = typer.confirm(
            f"{bird_conf_path} 已存在，覆盖？", default=False
        )

    overwrite_babel = True
    if bird_babel_conf_path.exists():
        overwrite_babel = typer.confirm(
            f"{bird_babel_conf_path} 已存在，覆盖？", default=False
        )

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

    if res.warnings:
        typer.echo("\n警告:")
        for w in res.warnings:
            typer.echo(f"- {w}")


@app.command("scan")
def cmd_scan(ctx: typer.Context) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    discovery = discover_bird_paths(
        candidate_bird_conf_paths=[
            Path(config.bird_conf_path),
            Path("/etc/bird/bird.conf"),
            Path("/etc/bird.conf"),
        ]
    )

    updated_config = config
    updated = False
    if discovery.bird_conf_path is not None and str(discovery.bird_conf_path) != str(
        config.bird_conf_path
    ):
        updated_config = replace(
            updated_config, bird_conf_path=str(discovery.bird_conf_path)
        )
        updated = True
    if discovery.bird_peers_dir is not None and str(discovery.bird_peers_dir) != str(
        config.bird_peers_dir
    ):
        updated_config = replace(
            updated_config, bird_peers_dir=str(discovery.bird_peers_dir)
        )
        updated = True
    if discovery.bird_babel_conf_path is not None and str(
        discovery.bird_babel_conf_path
    ) != str(config.bird_babel_conf_path):
        updated_config = replace(
            updated_config, bird_babel_conf_path=str(discovery.bird_babel_conf_path)
        )
        updated = True
    if discovery.bird_roa_v6_conf_path is not None and str(
        discovery.bird_roa_v6_conf_path
    ) != str(config.bird_roa_v6_conf_path):
        updated_config = replace(
            updated_config, bird_roa_v6_conf_path=str(discovery.bird_roa_v6_conf_path)
        )
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
        None, "--endpoint", help="Peer Endpoint (IP:Port)"
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
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    if peer_asn is None:
        peer_asn = typer.prompt("Peer ASN", type=int)
    if peer_public_key is None:
        peer_public_key = typer.prompt("Peer 公钥")
    if endpoint is None:
        endpoint = typer.prompt("Peer Endpoint (IP:Port)")
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA (fe80::...)")
    if net_backend is None:
        net_backend = typer.prompt("网络后端", default="networkd")

    assert peer_asn is not None
    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert net_backend is not None

    try:
        peer_public_key = _validate_pubkey(peer_public_key)
        endpoint = _validate_endpoint(endpoint)
        peer_lla = _validate_peer_lla(peer_lla)
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
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    try:
        db = appctx.open_db()
    except (PermissionError, OSError) as exc:
        typer.echo(f"错误: 权限不足/路径不可写 ({exc})")
        typer.echo(_db_open_hint(appctx.db_path))
        raise typer.Exit(1) from exc
    except DatabaseError as exc:
        typer.echo(f"错误: {exc}")
        typer.echo(_db_open_hint(appctx.db_path))
        raise typer.Exit(1) from exc
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
        peer_public_key = _validate_pubkey(peer_public_key)
        endpoint = _validate_endpoint(endpoint)
        if peer_lla:  # peer_lla may be empty string if user cleared it
            peer_lla = _validate_peer_lla(peer_lla)
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


ibgp_app = typer.Typer()


@ibgp_app.command("peer")
def cmd_ibgp_peer(
    ctx: typer.Context,
    name: str | None = typer.Option(
        None, "--name", help="Peer 名称 (用于文件名/接口名)"
    ),
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
        help="本端 ListenPort (0 表示不设置；留空则自动选择未占用端口)",
    ),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    if name is None:
        name = typer.prompt("iBGP peer 名称")
    if peer_public_key is None:
        peer_public_key = typer.prompt("Peer 公钥")
    if endpoint is None:
        endpoint = typer.prompt("Peer Endpoint (IP:Port)")
    if peer_lla is None:
        peer_lla = typer.prompt("Peer LLA (fe80::...)")
    if net_backend is None:
        net_backend = typer.prompt("网络后端", default="networkd")

    assert name is not None
    assert peer_public_key is not None
    assert endpoint is not None
    assert peer_lla is not None
    assert net_backend is not None

    try:
        peer_public_key = _validate_pubkey(peer_public_key)
        endpoint = _validate_endpoint(endpoint)
        peer_lla = _validate_peer_lla(peer_lla)
    except typer.BadParameter as exc:
        typer.echo(f"输入错误: {exc}")
        raise typer.Exit(2) from exc

    try:
        res = create_ibgp_peer(
            config=config,
            db_path=appctx.db_path,
            name=name,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend=net_backend,
            listen_port=listen_port,
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


app.add_typer(ibgp_app, name="ibgp")


show_app = typer.Typer()


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
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

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


@show_app.command("bgp")
def cmd_show_bgp(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    try:
        peers = show_bgp_peers(config=config, db_path=appctx.db_path, include_live=True)
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    if as_json:
        _print_json([asdict(x) for x in peers])
        return

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


@show_app.command("ibgp")
def cmd_show_ibgp(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

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

    if not peers:
        typer.echo("(空) 未找到任何 iBGP peer")
        return
    for p in peers:
        proto = f"ibgp_{p.name}"
        typer.echo(
            f"{p.name} proto={proto} ifname={p.ifname} backend={p.net_backend} port={p.listen_port}"
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


@show_app.command("all")
def cmd_show_all(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

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
    if not tunnels:
        typer.echo("(空) 未找到任何 WireGuard 隧道")
    else:
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

    typer.echo("\n== bgp ==")
    if not bgp:
        typer.echo("(空) 未找到任何 BGP peer")
    else:
        for p in bgp:
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
                typer.echo(
                    f"  live(birdc): {'OK' if p.live_bird.ok else 'UNAVAILABLE'}"
                )

    typer.echo("\n== ibgp ==")
    if not ibgp:
        typer.echo("(空) 未找到任何 iBGP peer")
    else:
        for p in ibgp:
            proto = f"ibgp_{p.name}"
            typer.echo(
                f"{p.name} proto={proto} ifname={p.ifname} backend={p.net_backend} port={p.listen_port}"
            )
            typer.echo(f"  peer_lla: {p.peer_lla or ''}")
            typer.echo(f"  endpoint: {p.endpoint or ''}")
            typer.echo(f"  peer_pubkey: {p.peer_public_key or ''}")
            typer.echo(f"  wg_pubkey(local): {p.wg_public_key}")
            _print_file_statuses([asdict(f) for f in p.files])
            if p.live_wg is not None:
                typer.echo(f"  live(wg): {'OK' if p.live_wg.ok else 'UNAVAILABLE'}")
            if p.live_bird is not None:
                typer.echo(
                    f"  live(birdc): {'OK' if p.live_bird.ok else 'UNAVAILABLE'}"
                )


app.add_typer(show_app, name="show")


del_app = typer.Typer()
del_peer_app = typer.Typer()


@del_peer_app.command("bgp")
def cmd_del_peer_bgp(
    ctx: typer.Context,
    peer_asn: int = typer.Argument(..., help="Peer ASN"),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

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


@del_peer_app.command("ibgp")
def cmd_del_peer_ibgp(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="iBGP peer name"),
) -> None:
    appctx: AppContext = ctx.obj
    try:
        config = appctx.require_config()
    except FileNotFoundError as exc:
        typer.echo("错误: 未初始化，请先运行 dn42ctl init")
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        typer.echo(f"错误: 配置文件读取失败: {exc}")
        raise typer.Exit(2) from exc

    if not typer.confirm(
        f"确认删除 iBGP peer '{name}'（DB + 配置文件 + 重生成 babel.conf）？",
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


del_app.add_typer(del_peer_app, name="peer")
app.add_typer(del_app, name="del")
