"""Spoke-side `dn42ctl node apply`: turn a cached desired-state into actual
files under /etc/bird and /etc/systemd/network.

Reuses the existing Jinja renderers. Normally only the per-peer files and
babel.conf are touched.

If and only if the desired state carries a non-empty `node` block, apply also
rewrites `config.toml`, re-renders `bird.conf` and rewrites `dn42-dummy.*`. For a
node without that block, the set of written files is byte-for-byte identical to
what it was before this feature landed. See
`docs/architecture/node_addressing.md` for the semantics.

Atomic writes (tmp + rename) ensure we never leave a half-written file behind.
The one exception is a target the sandbox exposes as a single bind-mounted file,
where rename is impossible and `_write_in_place` takes over. Once the files are on
disk, networkd and bird are reloaded based on which paths actually changed; that
step is best-effort and a failure only records a warning.
"""

from __future__ import annotations

import dataclasses
import difflib
import errno
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dn42ctl.config import AppConfig, ConfigError, dumps_config, load_config
from dn42ctl.constants import (
    FILE_MODE_BIRD,
    FILE_MODE_NETDEV,
    FILE_MODE_PRIVATE,
    NET_BACKEND_NETWORKD,
    RELOAD_POLICY_NEVER,
)
from dn42ctl.fs import chmod_best_effort
from dn42ctl.node_config import NodeConfig
from dn42ctl.paths import (
    DEFAULT_BIRD_BABEL_CONF_PATH,
    DEFAULT_BIRD_CONF_PATH,
    DEFAULT_BIRD_EXTRA_CONF_PATH,
    DEFAULT_BIRD_PEERS_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_NETWORKD_DIR,
    DEFAULT_NM_SYSTEM_CONNECTIONS_DIR,
)
from dn42ctl.render import (
    render_babel_conf,
    render_bird_bgp_peer_conf,
    render_bird_extra_conf_placeholder,
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
    bird_extra_conf_path: Path
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


# config.toml 的 [paths] 键名与 ResolvedPaths 的字段名不一致,这里是唯一把两者对上的
# 地方: (ResolvedPaths 的字段, AppConfig 的属性, 内置默认值)。
_PATH_SOURCES: tuple[tuple[str, str, Path], ...] = (
    ("bird_peers_dir", "bird_peers_dir", DEFAULT_BIRD_PEERS_DIR),
    ("babel_conf_path", "bird_babel_conf_path", DEFAULT_BIRD_BABEL_CONF_PATH),
    ("networkd_dir", "networkd_dir", DEFAULT_NETWORKD_DIR),
    ("nm_dir", "nm_system_connections_dir", DEFAULT_NM_SYSTEM_CONNECTIONS_DIR),
    ("bird_conf_path", "bird_conf_path", DEFAULT_BIRD_CONF_PATH),
    ("bird_extra_conf_path", "bird_extra_conf_path", DEFAULT_BIRD_EXTRA_CONF_PATH),
)


def _resolve_paths(app_config: AppConfig | None, config_path: Path) -> ResolvedPaths:
    """Decide where this node writes: its own config.toml, else the built-in defaults.

    Same source as `genconf`, so the two outputs cannot drift apart. A node with no
    config.toml has no opinion and takes the defaults; that is not a degradation and
    is not reported.
    """
    fields = {
        field_name: Path(getattr(app_config, attr)) if app_config is not None else default
        for field_name, attr, default in _PATH_SOURCES
    }
    return ResolvedPaths(config_path=config_path, **fields)


def _write_in_place(path: Path, content: str, *, mode: int) -> None:
    """Overwrite through the existing inode, for targets that cannot be replaced.

    `ReadWritePaths=` on a single file bind-mounts it over an otherwise read-only
    /etc: the parent stays read-only so mkstemp fails, and the file is a mount point
    so rename onto it fails. A distro-default `/etc/bird.conf` is that case.

    Truncate and write go out back to back because this path gives up atomicity:
    dying in that window leaves a truncated file, and `bird.service` runs `bird -p`
    as ExecStartPre, so an empty bird.conf blocks the next boot until `genconf`
    rewrites it.
    """
    data = content.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    chmod_best_effort(path, mode)


# What mkstemp raises when the target's parent directory is read-only.
_NO_TMPFILE_ERRNOS = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    except OSError as exc:
        if exc.errno not in _NO_TMPFILE_ERRNOS or not path.exists():
            raise
        _write_in_place(path, content, mode=mode)
        return
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        chmod_best_effort(tmp, mode)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY or not path.exists():
                raise
            tmp.unlink(missing_ok=True)
            _write_in_place(path, content, mode=mode)
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
            FILE_MODE_BIRD,
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
    # render_bird_ibgp_peer_conf raises when peer_ip is empty. Skip just this one file
    # rather than failing the entire apply; genconf handles it the same way.
    if peer.get("peer_ip"):
        out.append(
            (
                paths.bird_peers_dir / f"ibgp_{name}.conf",
                render_bird_ibgp_peer_conf(name=name, ifname=ifname, peer_ip=peer["peer_ip"]),
                FILE_MODE_BIRD,
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
    app_config: AppConfig | None,
) -> tuple[list[tuple[Path, str, int]], list[str]]:
    """Turn the hub-pushed node addresses into config.toml / bird.conf / dn42-dummy.*.

    Without a local config.toml all three steps are skipped and a warning is recorded;
    a config is never fabricated. `bird.conf` needs the AS-level fields own_asn /
    ownnet_v6 / ownnetset_v6, which are outside what the hub pushes, and without them
    the template cannot render at all since it runs under StrictUndefined.
    """
    files: list[tuple[Path, str, int]] = []
    warnings: list[str] = []

    if app_config is None:
        warnings.append("中心下发了节点地址,但本机 config.toml 不可用,已跳过 config.toml/bird.conf/dummy")
        return files, warnings

    updates: dict[str, str] = {}
    for key in ("own_ipv6", "router_id"):
        value = node_block.get(key)
        if value:
            updates[key] = str(value)
    if not updates:
        return files, warnings

    merged = dataclasses.replace(app_config, **updates)
    # Compare first and write only on a real difference. save_config rewrites the whole
    # file through tomli_w, losing comments and unknown keys, so the normal path must
    # not touch this file at all.
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
                # Use the resolved paths, not the ones in config.toml: the peer files
                # and babel.conf are written to exactly these paths (desired-state paths
                # plus any node.toml [apply] overrides). Using config.toml's values would
                # make bird.conf include an empty directory.
                bird_babel_conf_path=paths.babel_conf_path,
                bird_peers_dir=paths.bird_peers_dir,
                bird_extra_conf_path=paths.bird_extra_conf_path,
                # The ROA file is not managed by apply, so it has no ResolvedPaths entry.
                bird_roa_v6_conf_path=Path(merged.bird_roa_v6_conf_path),
            ),
            FILE_MODE_BIRD,
        )
    )

    # bird.conf now includes extra.conf, so the file has to exist. Its content belongs to
    # the operator: putting the placeholder in the list unconditionally would let every
    # 900-second reconcile diff it against whatever they wrote and overwrite it.
    if not paths.bird_extra_conf_path.exists():
        files.append((paths.bird_extra_conf_path, render_bird_extra_conf_placeholder(), FILE_MODE_BIRD))

    if merged.dummy_backend != NET_BACKEND_NETWORKD:
        # When dn42-dummy is managed by NetworkManager, writing networkd's
        # .netdev/.network would produce a configuration that conflicts with NM. apply
        # also deliberately never shells out (nmcli belongs to ensure_dummy_interface),
        # so all it can do here is skip and warn: that node has to land the new address
        # via a local dn42ctl genconf/init.
        warnings.append(
            f"dummy_backend={merged.dummy_backend},dn42-dummy 由 NetworkManager 管理,"
            "本次未更新其地址;请在该节点上运行 dn42ctl genconf 使新 own_ipv6 生效"
        )
        return files, warnings

    # dn42-dummy goes into the list as an ordinary file entry rather than through
    # ensure_dummy_interface: that function shells out to networkctl/nmcli itself, which
    # would bypass the diff/dry-run machinery. Activation is left to the reload step.
    files.append((paths.networkd_dir / f"{DUMMY_IFNAME}.netdev", render_dummy_netdev(), FILE_MODE_NETDEV))
    files.append(
        (
            paths.networkd_dir / f"{DUMMY_IFNAME}.network",
            render_dummy_network(own_ipv6=merged.own_ipv6),
            FILE_MODE_NETDEV,
        )
    )
    return files, warnings


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
    return paths.babel_conf_path, render_babel_conf(interfaces=interfaces), FILE_MODE_BIRD


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
    config_path: Path = DEFAULT_CONFIG_PATH,
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
    node_id = node_config.node_id

    try:
        app_config: AppConfig | None = load_config(config_path)
    except ConfigError:
        app_config = None
    paths = _resolve_paths(app_config, config_path)
    warnings: list[str] = []

    files: list[tuple[Path, str, int]] = []
    for peer in payload.get("bgp_peers", []):
        files.extend(_render_bgp_peer_files(peer, paths, node_id))
    for peer in payload.get("ibgp_peers", []):
        files.extend(_render_ibgp_peer_files(peer, paths, node_id))
    files.append(_render_babel(payload, paths))

    # Backwards compatibility: for a node without a `node` block, the set of written
    # files is byte-for-byte what it was before this feature landed.
    node_block = payload.get("node") or {}
    if node_block:
        node_files, node_warnings = _render_node_config_files(node_block, paths, app_config)
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
            # Driven by which paths actually changed, not by "something was written":
            # the agent reconciles every 900 seconds, so reloading unconditionally would
            # mean 96 pointless birdc configure calls per node per day.
            touched = sorted(changed_actions | set(deleted))
            result = run_reloads(
                plan_reloads(
                    touched=touched,
                    networkd_dir=paths.networkd_dir,
                    bird_peers_dir=paths.bird_peers_dir,
                    bird_files=[paths.babel_conf_path, paths.bird_conf_path, paths.bird_extra_conf_path],
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
