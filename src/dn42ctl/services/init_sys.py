from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dn42ctl.config import AppConfig, save_config
from dn42ctl.render import render_bird_ibgp_peer_conf, render_bird_main_conf
from dn42ctl.services.core import (
    Dn42CtlError,
    GenConfResult,
    InitConfigResult,
    ensure_dir,
    open_db_and_ensure_node,
    parse_allowed_ips_json,
    regenerate_babel_conf,
    write_bird_bgp_peer,
    write_net_backend_files,
    write_text,
)
from dn42ctl.services.dummy import DummyResult, ensure_dummy_interface

DN42_ROA_V6_URL = "https://dn42.burble.com/roa/dn42_roa_bird2_6.conf"


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
    dummy_backend: str = "networkd",
) -> InitConfigResult:
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
        dummy_backend=dummy_backend,
    )
    try:
        save_config(config_path, config)
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法写入配置 {config_path}。") from exc
    except OSError as exc:
        raise Dn42CtlError(f"写入配置失败: {config_path} ({exc})") from exc

    db = open_db_and_ensure_node(db_path, node_id)

    dummy: DummyResult | None = None
    if sys.platform.startswith("linux"):
        dummy = ensure_dummy_interface(own_ipv6, backend=config.dummy_backend, networkd_dir=config.networkd_dir)

    return InitConfigResult(config=config, config_path=config_path, db_path=db_path, dummy=dummy)


def genconf(
    *,
    config: AppConfig,
    db_path: Path,
    overwrite_bird_conf: bool,
    overwrite_babel_conf: bool,
    regenerate_peers: bool = False,
) -> GenConfResult:
    node_id = config.node_id
    bird_conf_path = Path(config.bird_conf_path)
    bird_peers_dir = Path(config.bird_peers_dir)
    bird_babel_conf_path = Path(config.bird_babel_conf_path)
    bird_roa_v6_conf_path = Path(config.bird_roa_v6_conf_path)

    db = open_db_and_ensure_node(db_path, node_id)

    dummy: DummyResult | None = None
    if sys.platform.startswith("linux"):
        dummy = ensure_dummy_interface(config.own_ipv6, backend=config.dummy_backend, networkd_dir=config.networkd_dir)

    bird_conf_text = render_bird_main_conf(
        own_asn=config.own_asn,
        router_id=config.router_id,
        own_ipv6=config.own_ipv6,
        ownnet_v6=config.ownnet_v6,
        ownnetset_v6=config.ownnetset_v6,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_peers_dir=bird_peers_dir,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
    )

    if bird_conf_path.exists() and not overwrite_bird_conf:
        raise Dn42CtlError(f"Bird 主配置已存在且未允许覆盖: {bird_conf_path}")
    write_text(bird_conf_path, bird_conf_text)

    ensure_dir(bird_peers_dir)

    if bird_babel_conf_path.exists() and not overwrite_babel_conf:
        raise Dn42CtlError(f"babel.conf 已存在且未允许覆盖: {bird_babel_conf_path}")
    regenerate_babel_conf(config=config, db=db, node_id=node_id)

    warnings: list[str] = []

    # ROA v6: required by the Bird template filter; keep best-effort but explicit.
    if not bird_roa_v6_conf_path.exists():
        try:
            req = urllib.request.Request(
                DN42_ROA_V6_URL,
                headers={"User-Agent": "dn42ctl/0.1"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                content = resp.read().decode("utf-8", errors="replace")
            if not content.strip():
                raise ValueError("empty ROA content")
            write_text(bird_roa_v6_conf_path, content)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            warnings.append(f"ROA v6 下载失败: {exc}")
            warnings.append(
                "警告: ROA 文件为占位符（空表）。在 Bird 路由过滤中，"
                "未命中 ROA 的路由将被判定为 UNKNOWN 并拒绝导入，"
                f"请尽快手动获取: {DN42_ROA_V6_URL}"
            )
            placeholder = f"# dn42ctl: ROA v6 placeholder\n# 下载失败，请稍后手动获取: {DN42_ROA_V6_URL}\n"
            write_text(bird_roa_v6_conf_path, placeholder)

    generated_peer_files: list[Path] = []

    if regenerate_peers:
        has_networkd_peers = False

        for row in db.list_bgp_peers(node_id):
            ifname = row["ifname"]
            peer_lla = row["peer_lla"]
            peer_asn = row["peer_asn"]
            backend = row["net_backend"] or "networkd"

            write_bird_bgp_peer(
                config=config, ifname=ifname, peer_lla=peer_lla,
                peer_asn=peer_asn, generated=generated_peer_files,
            )
            write_net_backend_files(
                config=config,
                node_id=node_id,
                backend=backend,
                ifname=ifname,
                private_key=row["wg_private_key"],
                listen_port=row["listen_port"],
                peer_public_key=row["peer_public_key"],
                endpoint=row["endpoint"] or "",
                allowed_ips=parse_allowed_ips_json(row["allowed_ips_json"]),
                local_lla=row["local_lla"],
                peer_lla=peer_lla,
                generated=generated_peer_files,
            )
            if backend == "networkd":
                has_networkd_peers = True

        for row in db.list_ibgp_peers(node_id):
            ifname = row["ifname"]
            peer_name = row["name"]
            backend = row["net_backend"] or "networkd"
            peer_ip = row["peer_ip"] or ""
            has_wg = bool(row["has_wg"])

            bird_peer_path = bird_peers_dir / f"ibgp_{peer_name}.conf"
            bird_conf_text = render_bird_ibgp_peer_conf(
                name=peer_name, ifname=ifname, peer_ip=peer_ip,
            )
            write_text(bird_peer_path, bird_conf_text)
            generated_peer_files.append(bird_peer_path)

            if has_wg:
                write_net_backend_files(
                    config=config,
                    node_id=node_id,
                    backend=backend,
                    ifname=ifname,
                    private_key=row["wg_private_key"],
                    listen_port=row["listen_port"],
                    peer_public_key=row["peer_public_key"],
                    endpoint=row["endpoint"] or "",
                    allowed_ips=parse_allowed_ips_json(row["allowed_ips_json"]),
                    local_lla=row["local_lla"],
                    peer_lla=row["peer_lla"] or "",
                    generated=generated_peer_files,
                )
                if backend == "networkd":
                    has_networkd_peers = True

        if has_networkd_peers:
            try:
                subprocess.check_output(
                    ["networkctl", "reload"],
                    text=True,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                warnings.append(f"networkctl reload 失败: {exc}")

    return GenConfResult(
        config=config,
        db_path=db_path,
        bird_conf_path=bird_conf_path,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
        dummy=dummy,
        warnings=warnings,
        generated_peer_files=generated_peer_files,
    )
