"""Spoke-side `dn42ctl node apply`: turn a cached desired-state into actual
files under /etc/bird, /etc/systemd/network.

Reuses the existing Jinja renderers. Normally only touches per-peer files plus
babel.conf.

**当且仅当** desired state 带非空 `node` 块时，apply 还会重写 `config.toml`、重渲
`bird.conf`、重写 `dn42-dummy.*`。没有该块的节点，写入的文件集与本特性引入之前
逐字节一致。语义见 `docs/architecture/node_addressing.md`。

Atomic writes (tmp + rename) ensure we never leave a half-written file behind.
写盘之后按实际变更的路径 reload networkd/bird（best-effort，失败只记 warning）。
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dn42ctl.config import ConfigError, dumps_config, load_config
from dn42ctl.constants import FILE_MODE_NETDEV, FILE_MODE_PRIVATE, RELOAD_POLICY_NEVER
from dn42ctl.fs import chmod_best_effort
from dn42ctl.node_config import NodeConfig
from dn42ctl.paths import DEFAULT_CONFIG_PATH
from dn42ctl.render import (
    render_babel_conf,
    render_bird_bgp_peer_conf,
    render_bird_ibgp_peer_conf,
    render_bird_main_conf,
    render_dummy_netdev,
    render_dummy_network,
    render_networkd_netdev,
    render_networkd_network,
)
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.dummy import DUMMY_IFNAME
from dn42ctl.services.reload import (
    ReloadAction,
    Runner,
    plan_reloads,
    run_reloads,
)


@dataclass(frozen=True)
class ResolvedPaths:
    bird_peers_dir: Path
    babel_conf_path: Path
    networkd_dir: Path
    nm_dir: Path  # kept for stale-file cleanup of legacy .nmconnection files
    bird_conf_path: Path
    config_path: Path


@dataclass(frozen=True)
class ApplyDiff:
    path: Path
    action: str  # "create" | "update" | "unchanged" | "delete"
    diff: str  # unified diff, empty if unchanged


@dataclass(frozen=True)
class ApplyResult:
    revision: str
    diffs: list[ApplyDiff] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)
    reloads: list[ReloadAction] = field(default_factory=list)


def _resolve_paths(payload: dict[str, Any], node_config: NodeConfig) -> ResolvedPaths:
    """Merge desired-state.paths with node.toml [apply] overrides.

    Override key names (in node.toml [apply]) shadow the server's defaults.
    Recognized keys: bird_peers_dir, babel_conf_path, networkd_dir, nm_dir,
    bird_conf_path, config_path.
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
        bird_conf_path=Path(pick("bird_conf_path", "/etc/bird/bird.conf")),
        config_path=Path(pick("config_path", str(DEFAULT_CONFIG_PATH))),
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


def _render_bgp_peer_files(peer: dict[str, Any], paths: ResolvedPaths, node_id: str) -> list[tuple[Path, str, int]]:
    """Return [(path, content, mode), ...] for this BGP peer."""
    ifname = peer["ifname"]
    out: list[tuple[Path, str, int]] = []

    bird_path = paths.bird_peers_dir / f"{ifname}.conf"
    out.append(
        (
            bird_path,
            render_bird_bgp_peer_conf(ifname=ifname, peer_lla=peer["peer_lla"], peer_asn=int(peer["peer_asn"])),
            FILE_MODE_PRIVATE,
        )
    )

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
                local_lla=peer["local_lla"],
                peer_lla=peer["peer_lla"],
            ),
            FILE_MODE_PRIVATE,
        )
    )
    return out


def _render_ibgp_peer_files(peer: dict[str, Any], paths: ResolvedPaths, node_id: str) -> list[tuple[Path, str, int]]:
    name = peer["name"]
    ifname = peer["ifname"]
    out: list[tuple[Path, str, int]] = []
    # peer_ip 为空时 render_bird_ibgp_peer_conf 会抛异常。跳过这一个文件而不是让
    # **整个** apply 失败 —— genconf 也是这么处理的。
    if peer.get("peer_ip"):
        out.append(
            (
                paths.bird_peers_dir / f"ibgp_{name}.conf",
                render_bird_ibgp_peer_conf(name=name, ifname=ifname, peer_ip=peer["peer_ip"]),
                FILE_MODE_PRIVATE,
            )
        )
    if not peer["has_wg"]:
        return out
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
                local_lla=peer["local_lla"],
                peer_lla=peer.get("peer_lla") or "",
            ),
            FILE_MODE_PRIVATE,
        )
    )
    return out


def _render_node_config_files(
    node_block: dict[str, Any],
    paths: ResolvedPaths,
) -> tuple[list[tuple[Path, str, int]], list[str]]:
    """中心下发的节点自身地址 -> config.toml / bird.conf / dn42-dummy.*。

    本地没有 config.toml（或读不出来）时**跳过全部三步并告警**，绝不伪造一份：
    `bird.conf` 还需要 own_asn / ownnet_v6 / ownnetset_v6 这些不在下发范围内的
    AS 级字段，缺了就渲染不出来（模板跑在 StrictUndefined 下）。纯 spoke 完全可能
    只跑过 `node init` 而从没有过 config.toml。
    """
    files: list[tuple[Path, str, int]] = []
    warnings: list[str] = []

    try:
        app_config = load_config(paths.config_path)
    except ConfigError as exc:
        warnings.append(f"中心下发了节点地址,但本机 config.toml 不可用,已跳过 config.toml/bird.conf/dummy: {exc}")
        return files, warnings

    updates: dict[str, str] = {}
    for key in ("own_ipv6", "router_id"):
        value = node_block.get(key)
        if value:
            updates[key] = str(value)
    if not updates:
        return files, warnings

    merged = dataclasses.replace(app_config, **updates)
    # 先比较,有差异才写。save_config 用 tomli_w 整体重写,注释与未知键会丢 ——
    # 常规路径因此根本不碰这个文件。
    if merged != app_config:
        files.append((paths.config_path, dumps_config(merged), FILE_MODE_PRIVATE))

    files.append(
        (
            paths.bird_conf_path,
            render_bird_main_conf(
                own_asn=merged.own_asn,
                router_id=merged.router_id,
                own_ipv6=merged.own_ipv6,
                ownnet_v6=merged.ownnet_v6,
                ownnetset_v6=merged.ownnetset_v6,
                bird_babel_conf_path=Path(merged.bird_babel_conf_path),
                bird_peers_dir=Path(merged.bird_peers_dir),
                bird_roa_v6_conf_path=Path(merged.bird_roa_v6_conf_path),
            ),
            FILE_MODE_PRIVATE,
        )
    )
    # dn42-dummy 当作普通文件条目进列表,不调 ensure_dummy_interface —— 那个函数自己
    # shell out 到 networkctl/nmcli,会绕过 diff/dry-run 机制。生效交给 reload 步骤。
    files.append((paths.networkd_dir / f"{DUMMY_IFNAME}.netdev", render_dummy_netdev(), FILE_MODE_NETDEV))
    files.append(
        (
            paths.networkd_dir / f"{DUMMY_IFNAME}.network",
            render_dummy_network(own_ipv6=merged.own_ipv6),
            FILE_MODE_NETDEV,
        )
    )
    return files, warnings


def _render_config_toml(config: Any) -> str:  # noqa: ANN401 — AppConfig
    """把 AppConfig 渲染成 TOML 文本，好让它走与其它文件相同的原子写 + diff 管线。"""
    import io

    import tomli_w

    data: dict[str, Any] = {
        "node_id": config.node_id,
        "own_asn": config.own_asn,
        "router_id": config.router_id,
        "own_ipv6": config.own_ipv6,
        "ownnet_v6": config.ownnet_v6,
        "ownnetset_v6": config.ownnetset_v6,
        "dummy_backend": config.dummy_backend,
        "paths": {
            "bird_conf": config.bird_conf_path,
            "bird_peers_dir": config.bird_peers_dir,
            "bird_babel_conf": config.bird_babel_conf_path,
            "bird_roa_v6_conf": config.bird_roa_v6_conf_path,
            "networkd_dir": config.networkd_dir,
            "nm_system_connections_dir": config.nm_system_connections_dir,
        },
    }
    if config.dn42_registry_path is not None:
        data["dn42_registry_path"] = config.dn42_registry_path
    buf = io.BytesIO()
    tomli_w.dump(data, buf)
    return buf.getvalue().decode("utf-8")


def _render_babel(payload: dict[str, Any], paths: ResolvedPaths) -> tuple[Path, str, int]:
    interfaces: list[tuple[str, int, str]] = []
    for peer in payload.get("ibgp_peers", []):
        if not peer["has_wg"]:
            continue
        interfaces.append(
            (
                str(peer["ifname"]),
                int(peer["babel_rxcost"]),
                str(peer["babel_type"]),
            )
        )
    return paths.babel_conf_path, render_babel_conf(interfaces=interfaces), FILE_MODE_PRIVATE


def _managed_paths(paths: ResolvedPaths) -> list[tuple[Path, tuple[tuple[str, str], ...]]]:
    """Return (directory, (prefix, suffix)+) describing which files in each
    directory are managed by dn42ctl and therefore eligible for stale-deletion.

    Bird peers dir owns:    dn42_*.conf (BGP)  +  ibgp_*.conf (iBGP)
    networkd dir owns:      dn42_*.netdev/.network  +  wg_*.netdev/.network
    NM dir owns:            dn42_*.nmconnection  +  wg_*.nmconnection
    """
    return [
        (
            paths.bird_peers_dir,
            (("dn42_", ".conf"), ("ibgp_", ".conf")),
        ),
        (
            paths.networkd_dir,
            (
                ("dn42_", ".netdev"),
                ("dn42_", ".network"),
                ("wg_", ".netdev"),
                ("wg_", ".network"),
            ),
        ),
        (
            paths.nm_dir,
            (("dn42_", ".nmconnection"), ("wg_", ".nmconnection")),
        ),
    ]


def _is_managed(name: str, patterns: tuple[tuple[str, str], ...]) -> bool:
    return any(name.startswith(prefix) and name.endswith(suffix) for prefix, suffix in patterns)


def _collect_stale(expected: set[Path], paths: ResolvedPaths) -> list[Path]:
    """Find existing files matching dn42ctl's own naming under paths.* but not in `expected`."""
    stale: list[Path] = []
    for directory, patterns in _managed_paths(paths):
        try:
            entries = list(directory.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            if not _is_managed(entry.name, patterns):
                continue
            if entry in expected:
                continue
            stale.append(entry)
    return stale


def apply(
    *,
    node_config: NodeConfig,
    dry_run: bool = False,
    no_reload: bool = False,
    runner: Runner | None = None,
) -> ApplyResult:
    """Apply the cached desired state. Errors if no cache exists."""
    from dn42ctl.services.node_agent import read_cache

    cached = read_cache(node_config=node_config)
    if cached is None:
        raise Dn42CtlError("本地缓存为空,先运行 dn42ctl node pull")

    payload = cached.payload
    paths = _resolve_paths(payload, node_config)
    node_id = node_config.node_id
    warnings: list[str] = []

    files: list[tuple[Path, str, int]] = []
    for peer in payload.get("bgp_peers", []):
        files.extend(_render_bgp_peer_files(peer, paths, node_id))
    for peer in payload.get("ibgp_peers", []):
        files.extend(_render_ibgp_peer_files(peer, paths, node_id))
    files.append(_render_babel(payload, paths))

    # 兼容铰链:没有 node 块的节点,写入的文件集与本特性引入之前逐字节一致。
    node_block = payload.get("node") or {}
    if node_block:
        node_files, node_warnings = _render_node_config_files(node_block, paths)
        files.extend(node_files)
        warnings.extend(node_warnings)

    expected_paths: set[Path] = {path for path, _content, _mode in files}
    stale = _collect_stale(expected_paths, paths)

    diffs = [_diff(path, content) for path, content, _mode in files]
    for s in stale:
        diffs.append(ApplyDiff(path=s, action="delete", diff=""))

    written: list[Path] = []
    deleted: list[Path] = []
    reloads: list[ReloadAction] = []
    if not dry_run:
        changed_actions = {d.path for d in diffs if d.action in {"create", "update"}}
        for path, content, mode in files:
            _atomic_write(path, content, mode=mode)
            written.append(path)
        for s in stale:
            try:
                s.unlink()
                deleted.append(s)
            except FileNotFoundError:
                pass

        if no_reload or node_config.reload_policy == RELOAD_POLICY_NEVER:
            pass
        else:
            # 按**实际发生变化**的路径决定,而不是"写过就 reload":agent 每 900 秒
            # reconcile 一次,无脑 reload 会让每个节点每天无谓 birdc configure 96 次。
            touched = sorted(changed_actions | set(deleted))
            result = run_reloads(
                plan_reloads(
                    touched=touched,
                    networkd_dir=paths.networkd_dir,
                    bird_peers_dir=paths.bird_peers_dir,
                    bird_files=[paths.babel_conf_path, paths.bird_conf_path],
                ),
                runner=runner,
            )
            reloads = result.actions
            warnings.extend(result.warnings)

    return ApplyResult(
        revision=cached.revision,
        diffs=diffs,
        written=written,
        deleted=deleted,
        dry_run=dry_run,
        warnings=warnings,
        reloads=reloads,
    )


def apply_summary(result: ApplyResult) -> str:
    """Human-readable summary."""
    by_action: dict[str, int] = {"create": 0, "update": 0, "unchanged": 0, "delete": 0}
    for d in result.diffs:
        by_action[d.action] = by_action.get(d.action, 0) + 1
    suffix = " (dry-run)" if result.dry_run else ""
    parts = [
        f"revision={result.revision}{suffix}: "
        f"create={by_action['create']} update={by_action['update']} "
        f"unchanged={by_action['unchanged']} delete={by_action['delete']}"
    ]
    if result.reloads:
        parts.append("reload=" + ",".join(f"{' '.join(a.cmd)}{'' if a.ok else '(失败)'}" for a in result.reloads))
    if result.warnings:
        parts.append(f"warnings={len(result.warnings)}")
    return " ".join(parts)


def apply_diff_text(result: ApplyResult) -> str:
    """Verbose diff text suitable for --dry-run output."""
    parts: list[str] = []
    for d in result.diffs:
        if d.action == "unchanged":
            parts.append(f"= {d.path}")
        elif d.action == "create":
            parts.append(f"+ {d.path}  (新文件)")
        elif d.action == "delete":
            parts.append(f"- {d.path}  (stale, 将删除)")
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
