from __future__ import annotations

import configparser
import json
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dn42ctl.config import AppConfig, save_config
from dn42ctl.db import BgpPeerRecord, Database, DatabaseError, IbgpPeerRecord
from dn42ctl.render import (
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

DN42_ROA_V6_URL = "https://dn42.burble.com/roa/dn42_roa_bird2_6.conf"


class Dn42CtlError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandOutput:
    cmd: list[str]
    ok: bool
    output: str | None
    error: str | None


@dataclass(frozen=True)
class FileStatus:
    path: str
    exists: bool


@dataclass(frozen=True)
class BgpPeerView:
    peer_asn: int
    ifname: str
    peer_public_key: str | None
    endpoint: str | None
    peer_lla: str | None
    local_lla: str
    listen_port: int
    allowed_ips: list[str]
    net_backend: str
    wg_public_key: str
    files: list[FileStatus]
    live_wg: CommandOutput | None
    live_bird: CommandOutput | None


@dataclass(frozen=True)
class IbgpPeerView:
    name: str
    ifname: str
    peer_public_key: str | None
    endpoint: str | None
    peer_lla: str | None
    local_lla: str
    listen_port: int
    allowed_ips: list[str]
    net_backend: str
    wg_public_key: str
    files: list[FileStatus]
    live_wg: CommandOutput | None
    live_bird: CommandOutput | None


@dataclass(frozen=True)
class WgTunnelView:
    kind: str  # "bgp" | "ibgp"
    peer_asn: int | None
    name: str | None
    ifname: str
    peer_public_key: str | None
    endpoint: str | None
    allowed_ips: list[str]
    listen_port: int
    local_lla: str
    peer_lla: str | None
    net_backend: str
    wg_public_key: str
    files: list[FileStatus]
    live_wg: CommandOutput | None


@dataclass(frozen=True)
class DeleteResult:
    kind: str  # "bgp" | "ibgp"
    peer_asn: int | None
    name: str | None
    deleted_files: list[str]
    missing_files: list[str]
    regenerated_files: list[str]


@dataclass(frozen=True)
class ScanImported:
    kind: str  # "bgp" | "ibgp"
    key: str  # "AS4242..." | "name"
    ifname: str
    net_backend: str


@dataclass(frozen=True)
class ScanResult:
    inserted: list[ScanImported]
    conflicts: list[ScanImported]
    skipped: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class BirdPathsDiscovery:
    bird_conf_path: Path | None
    bird_peers_dir: Path | None
    bird_babel_conf_path: Path | None
    bird_roa_v6_conf_path: Path | None
    warnings: list[str]


def _permission_hint() -> str:
    return (
        "请确认当前用户对该路径有写权限；"
        "若使用默认系统路径，通常需要以 root 运行（sudo）。"
        "也可以通过 --config-path/--db-path 或 init 的路径参数覆盖输出目录。"
    )


@dataclass(frozen=True)
class InitConfigResult:
    config: AppConfig
    config_path: Path
    db_path: Path


@dataclass(frozen=True)
class GenConfResult:
    config: AppConfig
    db_path: Path
    bird_conf_path: Path
    bird_babel_conf_path: Path
    bird_roa_v6_conf_path: Path
    systemd_roa_timer_enabled: bool
    warnings: list[str]


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

    return InitConfigResult(config=config, config_path=config_path, db_path=db_path)


def genconf(
    *,
    config: AppConfig,
    db_path: Path,
    overwrite_bird_conf: bool,
    overwrite_babel_conf: bool,
) -> GenConfResult:
    node_id = config.node_id
    bird_conf_path = Path(config.bird_conf_path)
    bird_peers_dir = Path(config.bird_peers_dir)
    bird_babel_conf_path = Path(config.bird_babel_conf_path)
    bird_roa_v6_conf_path = Path(config.bird_roa_v6_conf_path)

    db = _open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
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
    _write_text(bird_conf_path, bird_conf_text)

    _ensure_dir(bird_peers_dir)

    # Regenerate babel.conf deterministically from DB iBGP peers.
    try:
        interface_names = [str(r["ifname"]) for r in db.list_ibgp_peers(node_id)]
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    babel_text = render_babel_conf(interface_names=interface_names)
    if bird_babel_conf_path.exists() and not overwrite_babel_conf:
        raise Dn42CtlError(f"babel.conf 已存在且未允许覆盖: {bird_babel_conf_path}")
    _write_text(bird_babel_conf_path, babel_text)

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
            _write_text(bird_roa_v6_conf_path, content)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            warnings.append(f"ROA v6 下载失败: {exc}")
            warnings.append(
                "警告: ROA 文件为占位符（空表）。在 Bird 路由过滤中，"
                "未命中 ROA 的路由将被判定为 UNKNOWN 并拒绝导入，"
                f"请尽快手动获取: {DN42_ROA_V6_URL}"
            )
            placeholder = (
                "# dn42ctl: ROA v6 placeholder\n"
                f"# 下载失败，请稍后手动获取: {DN42_ROA_V6_URL}\n"
            )
            _write_text(bird_roa_v6_conf_path, placeholder)

    systemd_enabled = False
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        unit_dir = Path("/etc/systemd/system")
        service_path = unit_dir / "dn42-roa-v6.service"
        timer_path = unit_dir / "dn42-roa-v6.timer"
        roa_target = bird_roa_v6_conf_path
        roa_parent = roa_target.parent

        if shutil.which("curl") is None:
            warnings.append("未找到 curl：systemd ROA 定时更新可能失败")

        service_text = (
            "[Unit]\n"
            "Description=Download DN42 ROA for BIRD2 (IPv6)\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            # Quote path to handle spaces/special characters safely.
            f"ExecStartPre=/usr/bin/mkdir -p '{roa_parent}'\n"
            f"ExecStart=curl -fsSL -o '{roa_target}' {DN42_ROA_V6_URL}\n"
            "ExecStartPost=-birdc configure\n"
        )
        timer_text = (
            "[Unit]\n"
            "Description=Daily timer to download DN42 ROA (IPv6)\n\n"
            "[Timer]\n"
            "OnCalendar=*-*-* 00:05:00 UTC\n"
            "Persistent=true\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

        try:
            _write_text(service_path, service_text)
            _write_text(timer_path, timer_text)
            subprocess.check_output(["systemctl", "daemon-reload"], text=True)
            subprocess.check_output(
                ["systemctl", "enable", "--now", "dn42-roa-v6.timer"], text=True
            )
            # Trigger once so the ROA file is available immediately.
            subprocess.check_output(
                ["systemctl", "start", "dn42-roa-v6.service"], text=True
            )
            systemd_enabled = True
        except PermissionError as exc:
            raise Dn42CtlError(
                f"权限不足: 无法安装/启用 systemd 定时器。{_permission_hint()}"
            ) from exc
        except FileNotFoundError as exc:
            # systemctl disappeared between which() and now; just warn.
            warnings.append(f"systemctl 不可用，跳过 ROA 定时器: {exc}")
        except subprocess.CalledProcessError as exc:
            out = exc.output.strip() if isinstance(exc.output, str) else ""
            warnings.append(f"systemd ROA 定时器启用失败: exit={exc.returncode} {out}")
    else:
        warnings.append("当前系统不支持 systemd：已跳过 ROA 定时器配置")

    return GenConfResult(
        config=config,
        db_path=db_path,
        bird_conf_path=bird_conf_path,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
        systemd_roa_timer_enabled=systemd_enabled,
        warnings=warnings,
    )


_BIRD_INCLUDE_RE = re.compile(
    r"^\s*include\s+([\"'])([^\"']+)\1\s*;\s*(?:#.*)?$",
    flags=re.MULTILINE,
)


def discover_bird_paths(
    *,
    candidate_bird_conf_paths: list[Path],
) -> BirdPathsDiscovery:
    """Best-effort parse bird.conf to infer include paths.

    The primary use is `scan`: detect non-standard peers/babel/roa locations.
    """

    warnings: list[str] = []

    def _try_read(path: Path) -> str | None:
        try:
            if not path.exists():
                return None
        except OSError as exc:
            warnings.append(f"无法访问 bird.conf: {path} ({exc})")
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            warnings.append(f"权限不足: 无法读取 bird.conf: {path}")
            return None
        except OSError as exc:
            warnings.append(f"读取 bird.conf 失败: {path} ({exc})")
            return None

    seen: set[str] = set()
    best: BirdPathsDiscovery | None = None

    for p in candidate_bird_conf_paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)

        text = _try_read(p)
        if text is None:
            continue

        peers_dir: Path | None = None
        babel_path: Path | None = None
        roa_v6_path: Path | None = None

        for m in _BIRD_INCLUDE_RE.finditer(text):
            inc = m.group(2).strip()
            if not inc:
                continue

            inc_path = Path(inc)
            name = inc_path.name
            normalized = inc.replace("\\", "/")

            if babel_path is None and name == "babel.conf":
                babel_path = inc_path
            if roa_v6_path is None and name == "roa_dn42_v6.conf":
                roa_v6_path = inc_path

            if peers_dir is None and "*" in inc:
                # Heuristic: prefer an include that clearly targets a peers dir.
                if "/peers/" in normalized or "/peers" in normalized:
                    peers_dir = inc_path.parent

        discovery = BirdPathsDiscovery(
            bird_conf_path=p,
            bird_peers_dir=peers_dir,
            bird_babel_conf_path=babel_path,
            bird_roa_v6_conf_path=roa_v6_path,
            warnings=[],
        )

        if peers_dir or babel_path or roa_v6_path:
            # Found useful paths; return immediately.
            return BirdPathsDiscovery(
                bird_conf_path=p,
                bird_peers_dir=peers_dir,
                bird_babel_conf_path=babel_path,
                bird_roa_v6_conf_path=roa_v6_path,
                warnings=warnings,
            )

        # Keep as fallback if we at least managed to read a candidate.
        if best is None:
            best = discovery

    if best is not None:
        return BirdPathsDiscovery(
            bird_conf_path=best.bird_conf_path,
            bird_peers_dir=best.bird_peers_dir,
            bird_babel_conf_path=best.bird_babel_conf_path,
            bird_roa_v6_conf_path=best.bird_roa_v6_conf_path,
            warnings=warnings,
        )

    return BirdPathsDiscovery(
        bird_conf_path=None,
        bird_peers_dir=None,
        bird_babel_conf_path=None,
        bird_roa_v6_conf_path=None,
        warnings=warnings,
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
    if listen_port is None:
        listen_port = int(as_last5)
        if listen_port > 65535:
            raise Dn42CtlError(f"由 ASN 推导的 ListenPort 超出范围: {listen_port}")
    else:
        if listen_port < 0 or listen_port > 65535:
            raise Dn42CtlError(f"ListenPort 超出范围 (0/1-65535): {listen_port}")

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
    listen_port: int | None = None,
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
    current_listen_port = int(row["listen_port"])
    new_listen_port = current_listen_port if listen_port is None else listen_port
    if new_listen_port < 0 or new_listen_port > 65535:
        raise Dn42CtlError(f"ListenPort 超出范围 (0/1-65535): {new_listen_port}")
    if listen_port is not None and new_listen_port > 0 and new_listen_port != current_listen_port:
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
    _write_text(bird_peer_path, bird_conf_text)
    generated.append(bird_peer_path)

    _write_net_backend_files(
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
    db = _open_db(db_path)
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
        listen_port = _pick_unused_port(used_ports)
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


_LIVE_CMD_TIMEOUT = 2  # seconds


def _run_cmd_best_effort(cmd: list[str]) -> CommandOutput:
    try:
        out = subprocess.check_output(
            cmd, text=True, stderr=subprocess.STDOUT, timeout=_LIVE_CMD_TIMEOUT
        ).strip()
        return CommandOutput(cmd=cmd, ok=True, output=out, error=None)
    except FileNotFoundError as exc:
        return CommandOutput(cmd=cmd, ok=False, output=None, error=str(exc))
    except subprocess.TimeoutExpired:
        return CommandOutput(cmd=cmd, ok=False, output=None, error="timeout")
    except subprocess.CalledProcessError as exc:
        output = exc.output.strip() if isinstance(exc.output, str) else None
        return CommandOutput(
            cmd=cmd, ok=False, output=output, error=f"exit={exc.returncode}"
        )


def _parse_allowed_ips_json(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_ALLOWED_IPS
    try:
        loaded: object = json.loads(raw)
    except json.JSONDecodeError:
        return DEFAULT_ALLOWED_IPS
    if isinstance(loaded, list):
        ips: list[str] = []
        for item in cast(list[object], loaded):
            if not isinstance(item, str):
                return DEFAULT_ALLOWED_IPS
            ips.append(item)
        return ips
    return DEFAULT_ALLOWED_IPS


def _file_status(paths: list[Path]) -> list[FileStatus]:
    out: list[FileStatus] = []
    for p in paths:
        try:
            exists = p.exists()
        except OSError:
            exists = False
        out.append(FileStatus(path=str(p), exists=exists))
    return out


def _peer_files_for_backend(
    *,
    config: AppConfig,
    ifname: str,
    net_backend: str,
    kind: str,
    ibgp_name: str | None = None,
) -> list[Path]:
    files: list[Path] = []
    bird_peers_dir = Path(config.bird_peers_dir)

    if kind == "bgp":
        files.append(bird_peers_dir / f"{ifname}.conf")
    elif kind == "ibgp":
        if ibgp_name is None:
            raise ValueError("ibgp_name is required for kind=ibgp")
        files.append(bird_peers_dir / f"ibgp_{ibgp_name}.conf")
        files.append(Path(config.bird_babel_conf_path))

    if net_backend == "networkd":
        netdir = Path(config.networkd_dir)
        files.extend([netdir / f"{ifname}.netdev", netdir / f"{ifname}.network"])
    elif net_backend == "nm":
        nmdir = Path(config.nm_system_connections_dir)
        files.append(nmdir / f"{ifname}.nmconnection")
    elif net_backend == "wgquick":
        # Imported legacy setups may store wg-quick configs here.
        files.append(Path("/etc/wireguard") / f"{ifname}.conf")

    return files


def show_bgp_peers(
    *,
    config: AppConfig,
    db_path: Path,
    include_live: bool = True,
) -> list[BgpPeerView]:
    db = _open_db(db_path)
    node_id = config.node_id
    try:
        rows = db.list_bgp_peers(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    out: list[BgpPeerView] = []
    for row in rows:
        ifname = str(row["ifname"])
        peer_asn = int(row["peer_asn"])
        net_backend = str(row["net_backend"])
        files = _peer_files_for_backend(
            config=config, ifname=ifname, net_backend=net_backend, kind="bgp"
        )
        live_wg = _run_cmd_best_effort(["wg", "show", ifname]) if include_live else None
        live_bird = (
            _run_cmd_best_effort(["birdc", "show", "protocols", ifname])
            if include_live
            else None
        )

        out.append(
            BgpPeerView(
                peer_asn=peer_asn,
                ifname=ifname,
                peer_public_key=row["peer_public_key"],
                endpoint=row["endpoint"],
                peer_lla=row["peer_lla"],
                local_lla=str(row["local_lla"]),
                listen_port=int(row["listen_port"]),
                allowed_ips=_parse_allowed_ips_json(
                    str(row["allowed_ips_json"]) if row["allowed_ips_json"] else None
                ),
                net_backend=net_backend,
                wg_public_key=str(row["wg_public_key"]),
                files=_file_status(files),
                live_wg=live_wg,
                live_bird=live_bird,
            )
        )
    return out


def show_ibgp_peers(
    *,
    config: AppConfig,
    db_path: Path,
    include_live: bool = True,
) -> list[IbgpPeerView]:
    db = _open_db(db_path)
    node_id = config.node_id
    try:
        rows = db.list_ibgp_peers(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    out: list[IbgpPeerView] = []
    for row in rows:
        name = str(row["name"])
        ifname = str(row["ifname"])
        net_backend = str(row["net_backend"])
        files = _peer_files_for_backend(
            config=config,
            ifname=ifname,
            net_backend=net_backend,
            kind="ibgp",
            ibgp_name=name,
        )
        proto = f"ibgp_{name}"
        live_wg = _run_cmd_best_effort(["wg", "show", ifname]) if include_live else None
        live_bird = (
            _run_cmd_best_effort(["birdc", "show", "protocols", proto])
            if include_live
            else None
        )
        out.append(
            IbgpPeerView(
                name=name,
                ifname=ifname,
                peer_public_key=row["peer_public_key"],
                endpoint=row["endpoint"],
                peer_lla=row["peer_lla"],
                local_lla=str(row["local_lla"]),
                listen_port=int(row["listen_port"]),
                allowed_ips=_parse_allowed_ips_json(
                    str(row["allowed_ips_json"]) if row["allowed_ips_json"] else None
                ),
                net_backend=net_backend,
                wg_public_key=str(row["wg_public_key"]),
                files=_file_status(files),
                live_wg=live_wg,
                live_bird=live_bird,
            )
        )
    return out


def show_wg_tunnels(
    *,
    config: AppConfig,
    db_path: Path,
    include_live: bool = True,
) -> list[WgTunnelView]:
    tunnels: list[WgTunnelView] = []
    for p in show_bgp_peers(config=config, db_path=db_path, include_live=include_live):
        tunnels.append(
            WgTunnelView(
                kind="bgp",
                peer_asn=p.peer_asn,
                name=None,
                ifname=p.ifname,
                peer_public_key=p.peer_public_key,
                endpoint=p.endpoint,
                allowed_ips=p.allowed_ips,
                listen_port=p.listen_port,
                local_lla=p.local_lla,
                peer_lla=p.peer_lla,
                net_backend=p.net_backend,
                wg_public_key=p.wg_public_key,
                files=p.files,
                live_wg=p.live_wg,
            )
        )
    for p in show_ibgp_peers(config=config, db_path=db_path, include_live=include_live):
        tunnels.append(
            WgTunnelView(
                kind="ibgp",
                peer_asn=None,
                name=p.name,
                ifname=p.ifname,
                peer_public_key=p.peer_public_key,
                endpoint=p.endpoint,
                allowed_ips=p.allowed_ips,
                listen_port=p.listen_port,
                local_lla=p.local_lla,
                peer_lla=p.peer_lla,
                net_backend=p.net_backend,
                wg_public_key=p.wg_public_key,
                files=p.files,
                live_wg=p.live_wg,
            )
        )
    return tunnels


def _unlink_best_effort(path: Path) -> bool:
    """Return True if deleted, False if missing."""
    try:
        path.unlink(missing_ok=True)
        return True
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法删除 {path}。{_permission_hint()}") from exc
    except IsADirectoryError as exc:
        raise Dn42CtlError(f"删除失败: {path} 是目录") from exc
    except OSError as exc:
        raise Dn42CtlError(f"删除失败: {path} ({exc})") from exc


def _delete_files_and_collect_status(
    files: list[Path],
) -> tuple[list[str], list[str]]:
    """Delete each path in *files*; return (deleted, missing) string lists."""
    deleted: list[str] = []
    missing: list[str] = []
    for p in files:
        existed = False
        try:
            existed = p.exists()
        except OSError:
            existed = False
        _unlink_best_effort(p)
        if existed:
            deleted.append(str(p))
        else:
            missing.append(str(p))
    return deleted, missing


def delete_bgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    peer_asn: int,
) -> DeleteResult:
    db = _open_db(db_path)
    node_id = config.node_id
    try:
        row = db.get_bgp_peer(node_id, peer_asn)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    if row is None:
        raise Dn42CtlError("该 BGP peer 不存在")

    ifname = str(row["ifname"])
    net_backend = str(row["net_backend"])
    files = _peer_files_for_backend(
        config=config, ifname=ifname, net_backend=net_backend, kind="bgp"
    )

    deleted, missing = _delete_files_and_collect_status(files)

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


def delete_ibgp_peer(
    *,
    config: AppConfig,
    db_path: Path,
    name: str,
) -> DeleteResult:
    db = _open_db(db_path)
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
    files = _peer_files_for_backend(
        config=config,
        ifname=ifname,
        net_backend=net_backend,
        kind="ibgp",
        ibgp_name=peer_name,
    )
    babel_path = Path(config.bird_babel_conf_path)
    files = [p for p in files if p != babel_path]

    deleted, missing = _delete_files_and_collect_status(files)

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
    _write_text(babel_path, babel_text)

    return DeleteResult(
        kind="ibgp",
        peer_asn=None,
        name=peer_name,
        deleted_files=deleted,
        missing_files=missing,
        regenerated_files=[str(babel_path)],
    )


def _is_managed_ifname(ifname: str) -> bool:
    return ifname.startswith("dn42_") or ifname.startswith("wg_")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法读取 {path}。{_permission_hint()}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"读取失败: {path} ({exc})") from exc


def _find_first(existing_paths: list[Path]) -> Path | None:
    for p in existing_paths:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def _parse_networkd_netdev(text: str) -> dict[str, object]:
    section = ""
    allowed_ips: list[str] = []
    out: dict[str, object] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        if section == "WireGuard":
            if key == "PrivateKey":
                out["private_key"] = val
            elif key == "ListenPort":
                try:
                    out["listen_port"] = int(val)
                except ValueError:
                    pass
        elif section == "WireGuardPeer":
            if key == "PublicKey":
                out["peer_public_key"] = val
            elif key == "Endpoint":
                out["endpoint"] = val
            elif key == "AllowedIPs":
                allowed_ips.append(val)
    if allowed_ips:
        out["allowed_ips"] = allowed_ips
    return out


def _parse_networkd_network(text: str) -> dict[str, object]:
    section = ""
    out: dict[str, object] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if section == "Address":
            if key == "Address":
                out["local_lla"] = val
            elif key == "Peer":
                out["peer_lla"] = val
    return out


def _parse_nmconnection(text: str) -> dict[str, object]:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    out: dict[str, object] = {}
    if cfg.has_section("wireguard"):
        out["private_key"] = cfg.get("wireguard", "private-key", fallback=None)
        try:
            out["listen_port"] = cfg.getint("wireguard", "listen-port", fallback=None)
        except ValueError:
            pass
        peers = cfg.get("wireguard", "peers", fallback="")
        peers = peers.strip()
        if peers:
            parts = peers.split()
            if parts:
                out["peer_public_key"] = parts[0]
            for part in parts[1:]:
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                if k == "endpoint":
                    out["endpoint"] = v
                elif k == "allowed-ips":
                    # NM list fields end with a trailing ';'
                    ips = [x for x in v.split(";") if x]
                    out["allowed_ips"] = ips

    if cfg.has_section("ipv6"):
        addr1 = cfg.get("ipv6", "address1", fallback=None)
        if addr1:
            out["local_lla"] = addr1
    return out


def _parse_wgquick_conf(text: str) -> dict[str, object]:
    section = ""
    seen_peer = False
    out: dict[str, object] = {}
    allowed_ips: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "peer":
                if seen_peer:
                    # Only import the first peer; dn42ctl assumes one peer per interface.
                    section = "peer_ignored"
                else:
                    seen_peer = True
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        val = val.strip()

        if section == "interface":
            if key == "privatekey":
                out["private_key"] = val
            elif key == "listenport":
                try:
                    out["listen_port"] = int(val)
                except ValueError:
                    pass
            elif key == "address":
                # Prefer the first IPv6 address for local LLA.
                first = val.split(",", 1)[0].strip()
                if first:
                    out["local_lla"] = first
        elif section == "peer":
            if key == "publickey":
                out["peer_public_key"] = val
            elif key == "endpoint":
                out["endpoint"] = val
            elif key == "allowedips":
                allowed_ips.extend([x.strip() for x in val.split(",") if x.strip()])

    if allowed_ips:
        out["allowed_ips"] = allowed_ips
    return out


def _parse_bird_bgp_peer_conf(text: str, ifname: str) -> tuple[int | None, str | None]:
    # neighbor <peer_lla>%<ifname> as <asn>;
    m = re.search(
        rf"neighbor\s+([^%\s]+)%{re.escape(ifname)}\s+as\s+(\d+)\s*;",
        text,
    )
    if not m:
        return None, None
    try:
        asn = int(m.group(2))
    except ValueError:
        asn = None
    peer_lla = m.group(1)
    return asn, peer_lla


def _parse_bird_ibgp_peer_conf(text: str, ifname: str) -> str | None:
    m = re.search(
        rf"neighbor\s+([^%\s]+)%{re.escape(ifname)}\s+as\s+OWNAS\s*;",
        text,
    )
    if not m:
        return None
    return m.group(1)


def _wg_pubkey_from_private(private_key: str) -> str:
    try:
        return subprocess.check_output(
            ["wg", "pubkey"], input=private_key, text=True, stderr=subprocess.STDOUT
        ).strip()
    except FileNotFoundError as exc:
        raise Dn42CtlError("未找到 'wg' 命令，请先安装 wireguard-tools") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.output.strip() if isinstance(exc.output, str) else ""
        raise Dn42CtlError(
            f"wg pubkey 执行失败 (exit={exc.returncode}){': ' + detail if detail else ''}"
        ) from exc


def scan_local_configs(*, config: AppConfig, db_path: Path) -> ScanResult:
    warnings: list[str] = []
    inserted: list[ScanImported] = []
    conflicts: list[ScanImported] = []
    skipped: list[str] = []

    if shutil.which("wg") is None:
        raise Dn42CtlError(
            "scan 需要 'wg' 命令以从私钥推导公钥，请先安装 wireguard-tools"
        )

    # Directories to scan (dedup while preserving intent).
    bird_peers_dirs = [
        Path(config.bird_peers_dir),
        Path("/etc/bird/peers"),
        Path("/etc/bird6/peers"),
    ]
    networkd_dirs = [Path(config.networkd_dir), Path("/etc/systemd/network")]
    nm_dirs = [
        Path(config.nm_system_connections_dir),
        Path("/etc/NetworkManager/system-connections"),
    ]
    wgquick_dirs = [Path("/etc/wireguard")]

    def _dedup(paths: list[Path]) -> list[Path]:
        seen: set[str] = set()
        out: list[Path] = []
        for p in paths:
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            out.append(p)
        return out

    bird_peers_dirs = _dedup(bird_peers_dirs)
    networkd_dirs = _dedup(networkd_dirs)
    nm_dirs = _dedup(nm_dirs)
    wgquick_dirs = _dedup(wgquick_dirs)

    # Candidate interfaces from known config file names.
    candidates: set[str] = set()

    def _collect_stems(dirs: list[Path], suffix: str) -> None:
        nonlocal candidates
        for d in dirs:
            try:
                if not d.exists():
                    continue
                for p in d.glob(f"*{suffix}"):
                    stem = p.name[: -len(suffix)]
                    if _is_managed_ifname(stem):
                        candidates.add(stem)
            except PermissionError:
                # Degrade gracefully: warn instead of aborting the whole scan.
                warnings.append(f"权限不足: 无法扫描目录 {d}，已跳过")
            except OSError as exc:
                warnings.append(f"扫描目录失败: {d} ({exc})")

    _collect_stems(networkd_dirs, ".netdev")
    _collect_stems(networkd_dirs, ".network")
    _collect_stems(nm_dirs, ".nmconnection")
    _collect_stems(wgquick_dirs, ".conf")

    db = _open_db(db_path)
    try:
        db.ensure_node(config.node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    for ifname in sorted(candidates):
        kind = "bgp" if ifname.startswith("dn42_") else "ibgp"
        peer_name = ifname[3:] if kind == "ibgp" else None

        # Locate config sources.
        netdev_path = _find_first([d / f"{ifname}.netdev" for d in networkd_dirs])
        network_path = _find_first([d / f"{ifname}.network" for d in networkd_dirs])
        nm_path = _find_first([d / f"{ifname}.nmconnection" for d in nm_dirs])
        wgquick_path = _find_first([d / f"{ifname}.conf" for d in wgquick_dirs])

        # Prefer supported backends.
        backend: str | None = None
        data: dict[str, object] = {}

        try:
            if netdev_path and network_path:
                backend = "networkd"
                data.update(_parse_networkd_netdev(_read_text(netdev_path)))
                data.update(_parse_networkd_network(_read_text(network_path)))
            elif nm_path:
                backend = "nm"
                try:
                    data.update(_parse_nmconnection(_read_text(nm_path)))
                except Exception as exc:  # configparser can raise various errors
                    skipped.append(f"{ifname}: 解析 NM 配置失败: {exc}")
                    continue
            elif wgquick_path:
                backend = "wgquick"
                data.update(_parse_wgquick_conf(_read_text(wgquick_path)))
            else:
                skipped.append(f"{ifname}: 未找到 networkd/NM/wg-quick 配置")
                continue
        except Dn42CtlError as exc:
            skipped.append(f"{ifname}: 读取配置失败: {exc}")
            continue

        private_key = str(data.get("private_key") or "").strip()
        if not private_key:
            skipped.append(f"{ifname}: 缺少 PrivateKey")
            continue

        raw_port = data.get("listen_port")
        listen_port: int = 0
        if isinstance(raw_port, int):
            listen_port = raw_port
        elif isinstance(raw_port, str):
            try:
                listen_port = int(raw_port.strip())
            except ValueError:
                listen_port = 0
        if listen_port <= 0:
            # ListenPort is optional for some setups (e.g. behind NAT/firewall).
            # Store 0 as a sentinel meaning "unset".
            warnings.append(f"{ifname}: 未找到 ListenPort，将以 0(未设置) 导入")
            listen_port = 0

        local_lla = str(data.get("local_lla") or "").strip()
        if not local_lla:
            skipped.append(f"{ifname}: 缺少本端 LLA/Address")
            continue

        peer_public_key = str(data.get("peer_public_key") or "").strip() or None
        endpoint = str(data.get("endpoint") or "").strip() or None
        allowed_ips_list: list[str]
        raw_allowed = data.get("allowed_ips")
        if isinstance(raw_allowed, list):
            collected: list[str] = []
            for item in cast(list[object], raw_allowed):
                if isinstance(item, str) and item:
                    collected.append(item)
            allowed_ips_list = collected or DEFAULT_ALLOWED_IPS
        else:
            allowed_ips_list = DEFAULT_ALLOWED_IPS

        peer_lla = str(data.get("peer_lla") or "").strip() or None

        # Bird conf is required for BGP ASN; optional for iBGP peer_lla.
        if kind == "bgp":
            bird_path = _find_first([d / f"{ifname}.conf" for d in bird_peers_dirs])
            if bird_path is None:
                skipped.append(f"{ifname}: 缺少 Bird peer conf，无法解析 ASN")
                continue
            asn, bird_peer_lla = _parse_bird_bgp_peer_conf(
                _read_text(bird_path), ifname
            )
            if asn is None:
                skipped.append(f"{ifname}: Bird peer conf 解析 ASN 失败")
                continue
            if peer_lla is None and bird_peer_lla:
                peer_lla = bird_peer_lla

            peer_key = f"AS{asn}"
            try:
                # Skip conflicts by default; user can delete then rescan.
                if db.get_bgp_peer(config.node_id, asn) is not None:
                    conflicts.append(
                        ScanImported(
                            kind="bgp", key=peer_key, ifname=ifname, net_backend=backend
                        )
                    )
                    continue
            except DatabaseError as exc:
                raise Dn42CtlError(str(exc)) from exc

            wg_public_key = _wg_pubkey_from_private(private_key)
            try:
                db.insert_bgp_peer(
                    BgpPeerRecord(
                        node_id=config.node_id,
                        peer_asn=asn,
                        ifname=ifname,
                        wg_private_key=private_key,
                        wg_public_key=wg_public_key,
                        peer_public_key=peer_public_key,
                        endpoint=endpoint,
                        local_lla=local_lla,
                        peer_lla=peer_lla,
                        listen_port=listen_port,
                        allowed_ips=allowed_ips_list,
                        net_backend=backend,
                    )
                )
                inserted.append(
                    ScanImported(
                        kind="bgp", key=peer_key, ifname=ifname, net_backend=backend
                    )
                )
            except DatabaseError as exc:
                # Keep it explicit; treat as conflict-like.
                conflicts.append(
                    ScanImported(
                        kind="bgp", key=peer_key, ifname=ifname, net_backend=backend
                    )
                )
                warnings.append(f"{ifname}: 写入 DB 失败: {exc}")
        else:
            assert peer_name is not None
            try:
                peer_name = sanitize_name(peer_name)
            except Dn42CtlError as exc:
                skipped.append(f"{ifname}: \u63a5\u53e3\u540d\u65e0\u6548: {exc}")
                continue

            # Optional: try bird conf to extract peer_lla.
            if peer_lla is None:
                bird_path = _find_first(
                    [d / f"ibgp_{peer_name}.conf" for d in bird_peers_dirs]
                )
                if bird_path is not None:
                    maybe = _parse_bird_ibgp_peer_conf(_read_text(bird_path), ifname)
                    if maybe:
                        peer_lla = maybe

            try:
                if db.get_ibgp_peer(config.node_id, peer_name) is not None:
                    conflicts.append(
                        ScanImported(
                            kind="ibgp",
                            key=peer_name,
                            ifname=ifname,
                            net_backend=backend,
                        )
                    )
                    continue
            except DatabaseError as exc:
                raise Dn42CtlError(str(exc)) from exc

            wg_public_key = _wg_pubkey_from_private(private_key)
            try:
                db.insert_ibgp_peer(
                    IbgpPeerRecord(
                        node_id=config.node_id,
                        name=peer_name,
                        ifname=ifname,
                        wg_private_key=private_key,
                        wg_public_key=wg_public_key,
                        peer_public_key=peer_public_key,
                        endpoint=endpoint,
                        local_lla=local_lla,
                        peer_lla=peer_lla,
                        listen_port=listen_port,
                        allowed_ips=allowed_ips_list,
                        net_backend=backend,
                    )
                )
                inserted.append(
                    ScanImported(
                        kind="ibgp",
                        key=peer_name,
                        ifname=ifname,
                        net_backend=backend,
                    )
                )
            except DatabaseError as exc:
                conflicts.append(
                    ScanImported(
                        kind="ibgp",
                        key=peer_name,
                        ifname=ifname,
                        net_backend=backend,
                    )
                )
                warnings.append(f"{ifname}: 写入 DB 失败: {exc}")

    if conflicts:
        warnings.append(
            "存在冲突（DB 已有记录）：默认已跳过。可先使用 'dn42ctl del peer ...' 删除后再 scan。"
        )

    return ScanResult(
        inserted=inserted,
        conflicts=conflicts,
        skipped=skipped,
        warnings=warnings,
    )
