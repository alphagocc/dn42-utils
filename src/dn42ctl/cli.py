from __future__ import annotations

import uuid
from pathlib import Path

import typer

from dn42ctl.context import AppContext
from dn42ctl.db import DatabaseError
from dn42ctl.config import ConfigError
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
    init_node,
    modify_bgp_peer,
)


app = typer.Typer(add_completion=False)


def _db_open_hint(db_path: Path) -> str:
    return (
        f"无法打开数据库: {db_path}。"
        "若使用默认系统路径，通常需要以 root 运行（sudo），"
        "或使用 --db-path 覆盖到可写位置。"
    )


@app.callback()
def _main(
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

    if own_ipv6 is None:
        own_ipv6 = existing.own_ipv6 if existing else typer.prompt("OWNIPv6")
    if len(own_ipv6) == 4 and all(c in "0123456789abcdefABCDEF" for c in own_ipv6):
        own_ipv6 = f"fddf:8aef:1053::{own_ipv6.lower()}"

    if ownnet_v6 is None:
        ownnet_v6 = (
            existing.ownnet_v6
            if existing
            else typer.prompt("OWNNETv6", default="fddf:8aef:1053::/48")
        )
    if ownnetset_v6 is None:
        ownnetset_v6 = (
            existing.ownnetset_v6
            if existing
            else typer.prompt("OWNNETSETv6", default=f"[{ownnet_v6}+]")
        )

    router_id = (
        existing.router_id
        if existing
        else typer.prompt("ROUTERID", default="169.254.0.1")
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
        result = init_node(
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
            overwrite_bird_conf=overwrite_bird,
            overwrite_babel_conf=overwrite_babel,
        )
    except Dn42CtlError as exc:
        typer.echo(f"错误: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("初始化完成")
    typer.echo(f"node_id: {result.config.node_id}")
    typer.echo(f"Bird: {result.bird_conf_path}")
    typer.echo(f"Babel: {result.bird_babel_conf_path}")
    typer.echo(f"DB: {result.db_path}")


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

    try:
        res = create_bgp_peer(
            config=config,
            db_path=appctx.db_path,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend=net_backend,
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

    try:
        res = modify_bgp_peer(
            config=config,
            db_path=appctx.db_path,
            peer_asn=peer_asn,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend=net_backend,
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

    try:
        res = create_ibgp_peer(
            config=config,
            db_path=appctx.db_path,
            name=name,
            peer_public_key=peer_public_key,
            endpoint=endpoint,
            peer_lla=peer_lla,
            net_backend=net_backend,
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
