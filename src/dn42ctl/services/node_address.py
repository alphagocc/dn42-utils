"""Central management of node addresses: propagation planning and writes.

A node's address shows up denormalised in *other* nodes' `ibgp_peers` rows, as the
host part of `endpoint` and as `peer_ip`. This module keeps those copies in step
whenever a node's address changes.

The pure computation (`plan_propagation`) is kept separate from the DB writes; it
takes plain dicts, which makes it easy to unit-test in a table-driven way.

For the rules, the invariants, and the cases that cannot be derived automatically and
are therefore left to a human, see `docs/architecture/node_addressing.md`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dn42ctl.constants import UNSET, _Unset
from dn42ctl.db import Database, emit_sync_event
from dn42ctl.db_managed import ManagedNode, ManagedNodeStore, PropagatedChange
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.validators import (
    ValidationError,
    format_endpoint,
    split_endpoint,
    validate_endpoint_host,
    validate_ipv6_address,
    validate_router_id,
)


@dataclass(frozen=True)
class NodeAddressUpdate:
    node: ManagedNode
    changes: list[PropagatedChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


def plan_propagation(
    *,
    rows: Sequence[Any],
    own_ipv6: str | None,
    endpoint_host: str | None,
) -> tuple[list[PropagatedChange], list[str]]:
    """Work out how the iBGP rows pointing at a node need to change.

    `rows` is every row in `ibgp_peers` whose `remote_node_id` points at that node,
    across all partitions. `own_ipv6` / `endpoint_host` are the node's new values;
    None means the field is not centrally managed.
    """
    changes: list[PropagatedChange] = []
    warnings: list[str] = []

    for row in rows:
        label = f"{row['node_id']}/{row['name']}"

        # peer_ip follows the target node's own_ipv6, but only for a non-empty value:
        # clearing own_ipv6 must never blank out peer_ip, because
        # render_bird_ibgp_peer_conf raises on an empty peer_ip and that would fail the
        # peer's entire apply.
        if own_ipv6 and row["peer_ip"] != own_ipv6:
            changes.append(
                PropagatedChange(
                    node_id=row["node_id"],
                    name=row["name"],
                    field="peer_ip",
                    old=row["peer_ip"],
                    new=own_ipv6,
                )
            )

        if not endpoint_host:
            continue

        current = (row["endpoint"] or "").strip()
        if not current:
            # A passive side never dials out, so there is no port to preserve and no
            # endpoint that could be derived. Leave it empty and say so.
            warnings.append(f"{label}: endpoint 为空(被动侧),无法推导端口,已跳过")
            continue
        try:
            _host, port = split_endpoint(current)
        except ValidationError:
            warnings.append(f"{label}: endpoint 无法解析 ({current!r}),已跳过")
            continue
        # Swap the host only and keep the port verbatim: behind NAT port mapping the
        # port legitimately differs from the peer's listen_port, and "fixing" it would
        # break exactly the deployments that are hardest to debug.
        new_endpoint = format_endpoint(endpoint_host, port)
        if new_endpoint != current:
            changes.append(
                PropagatedChange(
                    node_id=row["node_id"],
                    name=row["name"],
                    field="endpoint",
                    old=current,
                    new=new_endpoint,
                )
            )

    return changes, warnings


def _validated(
    value: str | None | _Unset,
    validator: Any,
    **kwargs: Any,
) -> str | None | _Unset:
    """Pass UNSET and None through unchanged; hand anything else to the validator."""
    if isinstance(value, _Unset) or value is None:
        return value
    try:
        return validator(value, **kwargs)
    except ValidationError as exc:
        raise Dn42CtlError(str(exc)) from exc


def set_node_addresses(
    *,
    db_path: Path,
    node_id: str,
    name: str | _Unset = UNSET,
    enabled: bool | _Unset = UNSET,
    auto_peer: bool | _Unset = UNSET,
    endpoint_host: str | None | _Unset = UNSET,
    own_ipv6: str | None | _Unset = UNSET,
    router_id: str | None | _Unset = UNSET,
    propagate: bool = True,
    dry_run: bool = False,
) -> NodeAddressUpdate:
    """Update a node's addresses and optionally propagate the change across the mesh.

    UNSET means leave the field alone; None means clear it, handing the field back to
    the node's own local management.
    """
    endpoint_host = _validated(endpoint_host, validate_endpoint_host)
    own_ipv6 = _validated(own_ipv6, validate_ipv6_address, field_name="own_ipv6")
    router_id = _validated(router_id, validate_router_id)

    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        node = store.get(node_id)
        if node is None:
            raise Dn42CtlError(f"managed node 不存在: {node_id}")

        # Propagation uses the new values; a field that was not passed falls back to
        # whatever is already in the DB.
        effective_ipv6 = node.own_ipv6 if isinstance(own_ipv6, _Unset) else own_ipv6
        effective_host = node.endpoint_host if isinstance(endpoint_host, _Unset) else endpoint_host

        changes: list[PropagatedChange] = []
        warnings: list[str] = []
        if propagate:
            rows = db.list_ibgp_peers_by_remote(node_id)
            changes, warnings = plan_propagation(
                rows=rows,
                own_ipv6=effective_ipv6,
                endpoint_host=effective_host,
            )
            if not rows and (effective_ipv6 or effective_host):
                warnings.append(f"没有任何 iBGP peer 行的 remote_node_id 指向 {node_id},本次改动不会传播到 mesh")
            if effective_ipv6 is None and rows:
                warnings.append("own_ipv6 未设置,指向该节点的 peer_ip 保持原值")

        if dry_run:
            return NodeAddressUpdate(node=node, changes=changes, warnings=warnings, dry_run=True)

        updated = store.apply_address_update(
            node_id,
            name=name,
            enabled=enabled,
            auto_peer=auto_peer,
            endpoint_host=endpoint_host,
            own_ipv6=own_ipv6,
            router_id=router_id,
            changes=changes,
        )
    finally:
        db.close()

    return NodeAddressUpdate(node=updated, changes=changes, warnings=warnings)


def backfill_remote_node_ids(*, db_path: Path, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Backfill remote_node_id from a unique `managed_nodes.own_ipv6 == ibgp_peers.peer_ip` match.

    Returns (descriptions of what was linked, descriptions of what was skipped). A row
    is skipped and reported whenever the match is ambiguous or absent: guessing the
    link wrong would make later address propagation rewrite the wrong node's config.
    """
    linked: list[str] = []
    skipped: list[str] = []
    db = Database.open(db_path)
    try:
        nodes = ManagedNodeStore(db.connection).list_all()
        by_ipv6: dict[str, list[str]] = {}
        for node in nodes:
            if node.own_ipv6:
                by_ipv6.setdefault(node.own_ipv6, []).append(node.node_id)

        rows: list[sqlite3.Row] = list(
            db.connection.execute(
                "SELECT node_id, name, peer_ip, remote_node_id FROM ibgp_peers ORDER BY node_id, name"
            )
        )
        pending: list[tuple[str, str, str]] = []
        for row in rows:
            label = f"{row['node_id']}/{row['name']}"
            if row["remote_node_id"]:
                skipped.append(f"{label}: 已有 remote_node_id,未改动")
                continue
            peer_ip = row["peer_ip"]
            if not peer_ip:
                skipped.append(f"{label}: peer_ip 为空,无法匹配")
                continue
            candidates = by_ipv6.get(peer_ip, [])
            if len(candidates) != 1:
                reason = "没有匹配的受管节点" if not candidates else f"匹配到 {len(candidates)} 个节点,不唯一"
                skipped.append(f"{label}: {reason} (peer_ip={peer_ip})")
                continue
            pending.append((row["node_id"], row["name"], candidates[0]))
            linked.append(f"{label} -> {candidates[0]}")

        if pending and not dry_run:
            try:
                for owner, name, target in pending:
                    db.connection.execute(
                        "UPDATE ibgp_peers SET remote_node_id=? WHERE node_id=? AND name=?",
                        (target, owner, name),
                    )
                db.connection.commit()
            except sqlite3.Error as exc:
                db.connection.rollback()
                raise Dn42CtlError("回填 remote_node_id 失败") from exc
    finally:
        db.close()
    return linked, skipped


@dataclass(frozen=True)
class AdoptSelfResult:
    """Result of re-homing peer rows from a stale partition onto the self node."""

    from_node_id: str
    to_node_id: str
    bgp_moved: int
    ibgp_moved: int
    dry_run: bool = False


def adopt_self_partition(
    *,
    db_path: Path,
    config_node_id: str,
    from_node_id: str | None = None,
    dry_run: bool = False,
) -> AdoptSelfResult:
    """Repair a deployment where `config.toml`'s node_id diverged from the self node id.

    While they were diverged, admin wrote peers under the `config.node_id` partition
    while the desired state was read via `managed_nodes.is_self`, so those peers were
    never pushed and nothing reported an error. This re-homes them onto the self node
    in a single transaction.

    The operation refuses to run when the target partition is non-empty:
    `UNIQUE(node_id, ifname)` would make the move fail halfway through, and rows on
    both sides mean someone has already written config under the new partition, where
    only a human can decide the merge strategy.
    """
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        self_node = store.get_self()
        if self_node is None:
            raise Dn42CtlError("没有 self 节点(managed_nodes.is_self=1),无需也无法执行 adopt-self")
        target = self_node.node_id
        source = from_node_id or config_node_id
        if source == target:
            raise Dn42CtlError(f"源分区与 self 节点相同 ({target}),无需修复")

        bgp_rows = db.list_bgp_peers(source)
        ibgp_rows = db.list_ibgp_peers(source)
        if not bgp_rows and not ibgp_rows:
            raise Dn42CtlError(f"源分区 {source} 下没有任何 peer 行,无需修复")

        if db.list_bgp_peers(target) or db.list_ibgp_peers(target):
            raise Dn42CtlError(f"目标分区 {target} 非空,拒绝自动搬迁(会撞 UNIQUE(node_id, ifname));请人工合并")

        if not dry_run:
            try:
                db.connection.execute("UPDATE bgp_peers SET node_id=? WHERE node_id=?", (target, source))
                db.connection.execute("UPDATE ibgp_peers SET node_id=? WHERE node_id=?", (target, source))
                # The move changed the target node's desired state, so it must be pushed.
                emit_sync_event(db.connection, node_id=target)
                db.connection.commit()
            except sqlite3.Error as exc:
                db.connection.rollback()
                raise Dn42CtlError("搬迁 peer 行失败") from exc
    finally:
        db.close()

    return AdoptSelfResult(
        from_node_id=source,
        to_node_id=target,
        bgp_moved=len(bgp_rows),
        ibgp_moved=len(ibgp_rows),
        dry_run=dry_run,
    )


__all__ = [
    "AdoptSelfResult",
    "NodeAddressUpdate",
    "adopt_self_partition",
    "backfill_remote_node_ids",
    "plan_propagation",
    "set_node_addresses",
]
