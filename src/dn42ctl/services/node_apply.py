"""Spoke-side `dn42ctl node apply`: turn a cached desired-state into actual
files under /etc/bird, /etc/systemd/network, or /etc/NetworkManager.

Reuses the existing Jinja renderers. Does NOT generate the top-level bird.conf
(which depends on local AppConfig fields like own_asn/router_id and is the
responsibility of `dn42ctl genconf`); apply only touches per-peer files plus
babel.conf.

Atomic writes (tmp + rename) ensure we never leave a half-written file behind.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dn42ctl.constants import FILE_MODE_NETDEV, FILE_MODE_PRIVATE
from dn42ctl.fs import chmod_best_effort
from dn42ctl.node_config import NodeConfig
from dn42ctl.render import (
    nm_uuid_for,
    render_babel_conf,
    render_bird_bgp_peer_conf,
    render_bird_ibgp_peer_conf,
    render_networkd_netdev,
    render_networkd_network,
    render_nmconnection_wireguard,
)
from dn42ctl.services.core import Dn42CtlError


@dataclass(frozen=True)
class ResolvedPaths:
    bird_peers_dir: Path
    babel_conf_path: Path
    networkd_dir: Path
    nm_dir: Path


@dataclass(frozen=True)
class ApplyDiff:
    path: Path
    action: str  # "create" | "update" | "unchanged"
    diff: str    # unified diff, empty if unchanged


@dataclass(frozen=True)
class ApplyResult:
    revision: str
    diffs: list[ApplyDiff] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    dry_run: bool = False


def _resolve_paths(payload: dict[str, Any], node_config: NodeConfig) -> ResolvedPaths:
    """Merge desired-state.paths with node.toml [apply] overrides.

    Override key names (in node.toml [apply]) shadow the server's defaults.
    Recognized keys: bird_peers_dir, babel_conf_path, networkd_dir, nm_dir,
    bird_conf_path (accepted but currently unused by apply).
    """
    defaults = payload.get("paths") or {}
    overrides = node_config.apply_overrides

    def pick(key: str, default: str) -> str:
        if key in overrides:
            return overrides[key]
        v = defaults.get(key)
        return v if isinstance(v, str) and v else default

    return ResolvedPaths(
        bird_peers_dir=Path(pick("peers_dir", "/etc/bird/peers/")),
        babel_conf_path=Path(pick("babel_conf_path", "/etc/bird/babel.conf")),
        networkd_dir=Path(pick("networkd_dir", "/etc/systemd/network/")),
        nm_dir=Path(pick("nm_dir", "/etc/NetworkManager/system-connections/")),
    )


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        chmod_best_effort(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _diff(path: Path, new_content: str) -> ApplyDiff:
    if not path.exists():
        return ApplyDiff(path=path, action="create", diff=new_content)
    old = path.read_text(encoding="utf-8")
    if old == new_content:
        return ApplyDiff(path=path, action="unchanged", diff="")
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new_content.splitlines(),
            fromfile=str(path),
            tofile=str(path) + " (new)",
            lineterm="",
        )
    )
    return ApplyDiff(path=path, action="update", diff=diff)


def _render_bgp_peer_files(
    peer: dict[str, Any], paths: ResolvedPaths, node_id: str
) -> list[tuple[Path, str, int]]:
    """Return [(path, content, mode), ...] for this BGP peer."""
    ifname = peer["ifname"]
    out: list[tuple[Path, str, int]] = []

    bird_path = paths.bird_peers_dir / f"{ifname}.conf"
    out.append(
        (
            bird_path,
            render_bird_bgp_peer_conf(
                ifname=ifname, peer_lla=peer["peer_lla"], peer_asn=int(peer["peer_asn"])
            ),
            FILE_MODE_PRIVATE,
        )
    )

    backend = peer["net_backend"]
    if backend == "networkd":
        out.append(
            (
                paths.networkd_dir / f"{ifname}.netdev",
                render_networkd_netdev(
                    ifname=ifname,
                    private_key=peer["wg_private_key"],
                    listen_port=int(peer["listen_port"]),
                    peer_public_key=peer["peer_public_key"],
                    endpoint=peer.get("endpoint") or "",
                    allowed_ips=peer["allowed_ips"],
                ),
                FILE_MODE_NETDEV,
            )
        )
        out.append(
            (
                paths.networkd_dir / f"{ifname}.network",
                render_networkd_network(
                    ifname=ifname,
                    local_lla_cidr=peer["local_lla"],
                    peer_lla=peer["peer_lla"],
                ),
                FILE_MODE_PRIVATE,
            )
        )
    elif backend == "nm":
        out.append(
            (
                paths.nm_dir / f"{ifname}.nmconnection",
                render_nmconnection_wireguard(
                    conn_id=ifname,
                    ifname=ifname,
                    conn_uuid=nm_uuid_for(node_id=node_id, ifname=ifname),
                    private_key=peer["wg_private_key"],
                    listen_port=int(peer["listen_port"]),
                    peer_public_key=peer["peer_public_key"],
                    endpoint=peer.get("endpoint") or "",
                    allowed_ips=peer["allowed_ips"],
                    local_ipv6_cidr=peer["local_lla"],
                ),
                FILE_MODE_PRIVATE,
            )
        )
    return out


def _render_ibgp_peer_files(
    peer: dict[str, Any], paths: ResolvedPaths, node_id: str
) -> list[tuple[Path, str, int]]:
    name = peer["name"]
    ifname = peer["ifname"]
    out: list[tuple[Path, str, int]] = []
    out.append(
        (
            paths.bird_peers_dir / f"ibgp_{name}.conf",
            render_bird_ibgp_peer_conf(name=name, ifname=ifname, peer_ip=peer["peer_ip"]),
            FILE_MODE_PRIVATE,
        )
    )
    if not peer.get("has_wg", True):
        return out
    backend = peer["net_backend"]
    if backend == "networkd":
        out.append(
            (
                paths.networkd_dir / f"{ifname}.netdev",
                render_networkd_netdev(
                    ifname=ifname,
                    private_key=peer["wg_private_key"],
                    listen_port=int(peer["listen_port"]),
                    peer_public_key=peer["peer_public_key"],
                    endpoint=peer.get("endpoint") or "",
                    allowed_ips=peer["allowed_ips"],
                ),
                FILE_MODE_NETDEV,
            )
        )
        out.append(
            (
                paths.networkd_dir / f"{ifname}.network",
                render_networkd_network(
                    ifname=ifname,
                    local_lla_cidr=peer["local_lla"],
                    peer_lla=peer.get("peer_lla") or "",
                ),
                FILE_MODE_PRIVATE,
            )
        )
    elif backend == "nm":
        out.append(
            (
                paths.nm_dir / f"{ifname}.nmconnection",
                render_nmconnection_wireguard(
                    conn_id=ifname,
                    ifname=ifname,
                    conn_uuid=nm_uuid_for(node_id=node_id, ifname=ifname),
                    private_key=peer["wg_private_key"],
                    listen_port=int(peer["listen_port"]),
                    peer_public_key=peer["peer_public_key"],
                    endpoint=peer.get("endpoint") or "",
                    allowed_ips=peer["allowed_ips"],
                    local_ipv6_cidr=peer["local_lla"],
                ),
                FILE_MODE_PRIVATE,
            )
        )
    return out


def _render_babel(payload: dict[str, Any], paths: ResolvedPaths) -> tuple[Path, str, int]:
    interfaces: list[tuple[str, int, str]] = []
    for peer in payload.get("ibgp_peers", []):
        if not peer.get("has_wg", True):
            continue
        interfaces.append(
            (
                str(peer["ifname"]),
                int(peer.get("babel_rxcost", 120)),
                str(peer.get("babel_type") or "tunnel"),
            )
        )
    return paths.babel_conf_path, render_babel_conf(interfaces=interfaces), FILE_MODE_PRIVATE


def apply(
    *,
    node_config: NodeConfig,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply the cached desired state. Errors if no cache exists."""
    from dn42ctl.services.node_agent import read_cache

    cached = read_cache(node_config=node_config)
    if cached is None:
        raise Dn42CtlError("本地缓存为空,先运行 dn42ctl node pull")

    payload = cached.payload
    paths = _resolve_paths(payload, node_config)
    node_id = node_config.node_id

    files: list[tuple[Path, str, int]] = []
    for peer in payload.get("bgp_peers", []):
        files.extend(_render_bgp_peer_files(peer, paths, node_id))
    for peer in payload.get("ibgp_peers", []):
        files.extend(_render_ibgp_peer_files(peer, paths, node_id))
    files.append(_render_babel(payload, paths))

    diffs = [_diff(path, content) for path, content, _mode in files]

    written: list[Path] = []
    if not dry_run:
        for path, content, mode in files:
            _atomic_write(path, content, mode=mode)
            written.append(path)

    return ApplyResult(revision=cached.revision, diffs=diffs, written=written, dry_run=dry_run)


def apply_summary(result: ApplyResult) -> str:
    """Human-readable summary."""
    by_action: dict[str, int] = {"create": 0, "update": 0, "unchanged": 0}
    for d in result.diffs:
        by_action[d.action] = by_action.get(d.action, 0) + 1
    suffix = " (dry-run)" if result.dry_run else ""
    return (
        f"revision={result.revision}{suffix}: "
        f"create={by_action['create']} update={by_action['update']} unchanged={by_action['unchanged']}"
    )


def apply_diff_text(result: ApplyResult) -> str:
    """Verbose diff text suitable for --dry-run output."""
    parts: list[str] = []
    for d in result.diffs:
        if d.action == "unchanged":
            parts.append(f"= {d.path}")
        elif d.action == "create":
            parts.append(f"+ {d.path}  (新文件)")
        else:
            parts.append(f"~ {d.path}")
            parts.append(d.diff)
    parts.append("---")
    parts.append(apply_summary(result))
    return "\n".join(parts)


__all__ = [
    "ApplyDiff",
    "ApplyResult",
    "apply",
    "apply_diff_text",
    "apply_summary",
]
