"""节点地址集中管理：传播计算与写入。

一个节点的"地址"会以反范式化的形式出现在**其他**节点的 `ibgp_peers` 行里
（`endpoint` 的主机部分、`peer_ip`）。本模块负责在节点地址变更时把这些副本改到位。

纯计算 (`plan_propagation`) 与 DB 写入分离，前者只吃 dict、便于表驱动单测。

规则、不变量与"无法自动推导因而留给人工"的情形，见
`docs/architecture/node_addressing.md`。
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
    """算出指向某节点的 iBGP 行需要怎么改。

    `rows` 是 `ibgp_peers` 里 `remote_node_id` 指向该节点的所有行（跨全部分区）。
    `own_ipv6` / `endpoint_host` 是该节点的**新**值；None 表示未纳入中心管理。
    """
    changes: list[PropagatedChange] = []
    warnings: list[str] = []

    for row in rows:
        label = f"{row['node_id']}/{row['name']}"

        # peer_ip <- 目标节点的 own_ipv6。只写非空值:清空 own_ipv6 绝不能把 peer_ip 抹掉,
        # render_bird_ibgp_peer_conf 对空 peer_ip 直接抛异常,那会让对端整个 apply 失败。
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
            # 被动侧从不主动拨号,没有端口可保留 —— 编不出 endpoint,留空并告知。
            warnings.append(f"{label}: endpoint 为空(被动侧),无法推导端口,已跳过")
            continue
        try:
            _host, port = split_endpoint(current)
        except ValidationError:
            warnings.append(f"{label}: endpoint 无法解析 ({current!r}),已跳过")
            continue
        # 只换 host,端口原样保留 —— NAT 端口映射下端口与对端 listen_port 本就合法地
        # 不一致,顺手"修正"会精确地弄坏最难排查的那类部署。
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
    """UNSET 与 None 原样通过；其余交给 validator。"""
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
    endpoint_host: str | None | _Unset = UNSET,
    own_ipv6: str | None | _Unset = UNSET,
    router_id: str | None | _Unset = UNSET,
    propagate: bool = True,
    dry_run: bool = False,
) -> NodeAddressUpdate:
    """更新节点地址并（可选）把改动传播到 mesh。

    UNSET = 该字段不改动；None = 清除该字段（交还节点本地管理）。
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

        # 传播用的是**新**值:未传的字段沿用库里现有的值。
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
            endpoint_host=endpoint_host,
            own_ipv6=own_ipv6,
            router_id=router_id,
            changes=changes,
        )
    finally:
        db.close()

    return NodeAddressUpdate(node=updated, changes=changes, warnings=warnings)


def backfill_remote_node_ids(*, db_path: Path, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """按 `managed_nodes.own_ipv6 == ibgp_peers.peer_ip` 唯一匹配回填 remote_node_id。

    返回 (已链接的描述, 跳过的描述)。匹配不唯一或匹配不到的行一律跳过并报告——
    猜错关联会让后续的地址传播改错节点的配置。
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
    """把 peer 行从失效分区重新挂到 self 节点的结果。"""

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
    """修复 `config.toml` 的 node_id 与 self 节点 id 分叉的存量部署。

    分叉时 admin 曾把 peer 写在 `config.node_id` 分区下，而 desired state 是按
    `managed_nodes.is_self` 读的——那些 peer 永远不会下发且没有任何报错。这里在一个
    事务里把它们重新挂到 self 节点。

    **目标分区非空时拒绝执行**：`UNIQUE(node_id, ifname)` 会让搬迁半途失败，而两边
    都有行意味着已经有人在新分区下写过配置，合并策略只能由人来定。
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
                # 搬迁后目标节点的 desired state 变了,必须推给它。
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
