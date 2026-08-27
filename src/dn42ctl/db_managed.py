from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from dn42ctl.constants import SYNC_EVENT_ACCESS_REVOKED, SYNC_EVENT_DESIRED, UNSET, _Unset
from dn42ctl.db import DatabaseError, emit_sync_event


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class PropagatedChange:
    """一条因节点地址变更而被改写的 iBGP peer 行。

    `node_id` 是**被改写行所属的节点**(从 B 看向 A 的那条记录里的 B),不是被编辑地址
    的那个节点。传播的计算在 services/node_address.py,这里只描述结果。
    """

    node_id: str
    name: str
    field: str  # "peer_ip" | "endpoint"
    old: str | None
    new: str


# 允许 _update_fields 写的列。列名拼进 SQL,必须是白名单,不能来自外部输入。
_UPDATABLE_NODE_COLUMNS = frozenset({"name", "enabled", "endpoint_host", "own_ipv6", "router_id"})
_PROPAGATABLE_PEER_COLUMNS = frozenset({"peer_ip", "endpoint"})


def _address_fields(
    *,
    endpoint_host: str | None | _Unset,
    own_ipv6: str | None | _Unset,
    router_id: str | None | _Unset,
) -> dict[str, object]:
    """把 UNSET 过滤掉,只留下真正要写的列。"""
    fields: dict[str, object] = {}
    if not isinstance(endpoint_host, _Unset):
        fields["endpoint_host"] = endpoint_host
    if not isinstance(own_ipv6, _Unset):
        fields["own_ipv6"] = own_ipv6
    if not isinstance(router_id, _Unset):
        fields["router_id"] = router_id
    return fields


DEFAULT_WRITE_POLICY: dict[str, str] = {
    "peer_add": "review",
    "peer_modify": "review",
    "peer_delete": "review",
    "report": "auto",
}


VALID_WRITE_POLICY_KEYS = frozenset(DEFAULT_WRITE_POLICY.keys())
VALID_POLICY_PEER_ADD = frozenset({"review", "auto_accept"})
VALID_POLICY_PEER_MODIFY = frozenset({"review"})
VALID_POLICY_PEER_DELETE = frozenset({"review"})
VALID_POLICY_REPORT = frozenset({"review", "auto"})


def validate_write_policy(policy: dict[str, str]) -> dict[str, str]:
    """Validate a write_policy dict; return a normalized copy with defaults filled in."""
    merged = dict(DEFAULT_WRITE_POLICY)
    for key, value in policy.items():
        if key not in VALID_WRITE_POLICY_KEYS:
            raise ValueError(f"未知 write_policy 字段: {key}")
        merged[key] = value
    if merged["peer_add"] not in VALID_POLICY_PEER_ADD:
        raise ValueError(f"peer_add 仅允许 {sorted(VALID_POLICY_PEER_ADD)}")
    if merged["peer_modify"] not in VALID_POLICY_PEER_MODIFY:
        raise ValueError("peer_modify 仅允许 'review'(防止节点被入侵后篡改权威记录)")
    if merged["peer_delete"] not in VALID_POLICY_PEER_DELETE:
        raise ValueError("peer_delete 仅允许 'review'(防止节点被入侵后抹除权威记录)")
    if merged["report"] not in VALID_POLICY_REPORT:
        raise ValueError(f"report 仅允许 {sorted(VALID_POLICY_REPORT)}")
    return merged


@dataclass(frozen=True)
class ManagedNode:
    node_id: str
    name: str
    api_token_hash: str | None
    write_policy: dict[str, str]
    enabled: bool
    is_self: bool
    last_seen_at: str | None
    created_at: str
    updated_at: str
    # 节点地址(v10)。None 表示"该字段不由中心管理":不下发,节点本地值原样保留。
    # 见 docs/architecture/node_addressing.md。
    endpoint_host: str | None = None
    own_ipv6: str | None = None
    router_id: str | None = None


def _row_to_managed_node(row: sqlite3.Row) -> ManagedNode:
    return ManagedNode(
        node_id=row["node_id"],
        name=row["name"],
        api_token_hash=row["api_token_hash"],
        write_policy=json.loads(row["write_policy"]),
        enabled=bool(row["enabled"]),
        is_self=bool(row["is_self"]),
        last_seen_at=row["last_seen_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        endpoint_host=row["endpoint_host"],
        own_ipv6=row["own_ipv6"],
        router_id=row["router_id"],
    )


_TOKEN_HASH_PREFIX = "sha256$"  # noqa: S105 — 算法标签,不是密码


def hash_token(token: str) -> str:
    return _TOKEN_HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(stored_hash: str, token: str) -> bool:
    return secrets.compare_digest(stored_hash, hash_token(token))


class ManagedNodeStore:
    """CRUD for the managed_nodes table.

    Receives an already-open sqlite3.Connection (typically from Database.connection)
    so that callers can share transactions with other stores.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, node_id: str, name: str) -> ManagedNode:
        now = _now_iso()
        try:
            # Ensure parent nodes row exists (FK target).
            self._conn.execute(
                """
                INSERT INTO nodes(node_id, created_at, updated_at) VALUES (?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET updated_at=excluded.updated_at
                """.strip(),
                (node_id, now, now),
            )
            self._conn.execute(
                """
                INSERT INTO managed_nodes(
                    node_id, name, api_token_hash, write_policy,
                    enabled, is_self, last_seen_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """.strip(),
                (
                    node_id,
                    name,
                    None,
                    json.dumps(DEFAULT_WRITE_POLICY, ensure_ascii=False),
                    1,
                    0,
                    None,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DatabaseError(f"节点已存在: {node_id}") from exc
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("插入 managed_node 失败") from exc

        node = self.get(node_id)
        if node is None:
            raise DatabaseError("插入后无法读取 managed_node")
        return node

    def get(self, node_id: str) -> ManagedNode | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM managed_nodes WHERE node_id=?",
                (node_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 managed_node 失败") from exc
        if row is None:
            return None
        return _row_to_managed_node(row)

    def list_all(self) -> list[ManagedNode]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM managed_nodes ORDER BY is_self DESC, name",
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("列出 managed_nodes 失败") from exc
        return [_row_to_managed_node(r) for r in rows]

    def get_self(self) -> ManagedNode | None:
        try:
            # ORDER BY 是防御性的:upsert_self 保证至多一行,但没有排序的 LIMIT 1 会在
            # 不变量万一被破坏时静默返回任意一行,而这一列决定所有 admin 写入的分区。
            row = self._conn.execute(
                "SELECT * FROM managed_nodes WHERE is_self=1 ORDER BY updated_at DESC, node_id DESC LIMIT 1",
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 self 节点失败") from exc
        if row is None:
            return None
        return _row_to_managed_node(row)

    # ---- self-registration (called by serve bootstrap) ----

    def upsert_self(self, node_id: str, name: str = "self") -> ManagedNode:
        """Insert or update the row marking this node as the central host's self node.

        Idempotent. Does not touch api_token_hash.

        Demotes any other `is_self=1` row first. Losing `/var/lib/dn42ctl/self_node_id`
        mints a fresh UUID, and without the demotion the old row keeps its flag: two
        self rows, and `get_self()` — which decides the partition every admin write
        lands in — has nothing to choose between them. Demote rather than delete; the
        old partition still holds every peer it ever had.
        """
        now = _now_iso()
        try:
            self._conn.execute(
                "UPDATE managed_nodes SET is_self=0, updated_at=? WHERE is_self=1 AND node_id<>?",
                (now, node_id),
            )
            self._conn.execute(
                """
                INSERT INTO nodes(node_id, created_at, updated_at) VALUES (?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET updated_at=excluded.updated_at
                """.strip(),
                (node_id, now, now),
            )
            self._conn.execute(
                """
                INSERT INTO managed_nodes(
                    node_id, name, api_token_hash, write_policy,
                    enabled, is_self, last_seen_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                    name=excluded.name,
                    is_self=1,
                    enabled=1,
                    updated_at=excluded.updated_at
                """.strip(),
                (
                    node_id,
                    name,
                    None,
                    json.dumps(DEFAULT_WRITE_POLICY, ensure_ascii=False),
                    1,
                    1,
                    None,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("upsert self 节点失败") from exc

        node = self.get(node_id)
        if node is None:
            raise DatabaseError("upsert 后无法读取 self 节点")
        return node

    def delete(self, node_id: str, *, force: bool = False) -> ManagedNode | None:
        existing = self.get(node_id)
        if existing is None:
            return None
        if existing.is_self and not force:
            raise DatabaseError("拒绝删除 self 节点 (传入 force=True 强制删除)")
        try:
            self._conn.execute("DELETE FROM managed_nodes WHERE node_id=?", (node_id,))
            # 节点没了,但通知必须活下来让 watcher 断开它的连接 —— 所以 sync_events 无 FK。
            emit_sync_event(self._conn, node_id=node_id, kind=SYNC_EVENT_ACCESS_REVOKED)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("删除 managed_node 失败") from exc
        return existing

    def set_token_hash(self, node_id: str, token_hash: str) -> None:
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE managed_nodes SET api_token_hash=?, updated_at=? WHERE node_id=?",
                (token_hash, now, node_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise DatabaseError(f"节点不存在: {node_id}")
            # 旧 token 立即失效 —— 踢掉该节点仍在用旧 token 的 WS 连接。
            emit_sync_event(self._conn, node_id=node_id, kind=SYNC_EVENT_ACCESS_REVOKED)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 token hash 失败") from exc

    def rotate_token(self, node_id: str, new_plaintext: str) -> None:
        """Hash new_plaintext and store it; idempotent across retries."""
        self.set_token_hash(node_id, hash_token(new_plaintext))

    def authenticate(self, token: str) -> ManagedNode | None:
        """Look up a node by Bearer token.

        Walks all enabled nodes with a non-null hash. The request carries no node
        identity, so there is nothing to index on; scanning every row also avoids
        leaking timing about which node_id is registered.
        """
        try:
            rows = self._conn.execute(
                "SELECT * FROM managed_nodes WHERE api_token_hash IS NOT NULL AND enabled=1",
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 managed_nodes (auth) 失败") from exc
        for row in rows:
            if verify_token(row["api_token_hash"], token):
                return _row_to_managed_node(row)
        return None

    def set_write_policy(self, node_id: str, policy: dict[str, str]) -> ManagedNode:
        normalized = validate_write_policy(policy)
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE managed_nodes SET write_policy=?, updated_at=? WHERE node_id=?",
                (json.dumps(normalized, ensure_ascii=False), now, node_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise DatabaseError(f"节点不存在: {node_id}")
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 write_policy 失败") from exc
        node = self.get(node_id)
        if node is None:  # pragma: no cover — checked rowcount above
            raise DatabaseError("更新后无法读取 managed_node")
        return node

    def touch_last_seen(self, node_id: str) -> None:
        now = _now_iso()
        try:
            self._conn.execute(
                "UPDATE managed_nodes SET last_seen_at=? WHERE node_id=?",
                (now, node_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 last_seen_at 失败") from exc

    def set_name(self, node_id: str, name: str) -> ManagedNode:
        return self._update_fields(node_id, {"name": name}, events=())

    def set_enabled(self, node_id: str, enabled: bool) -> ManagedNode:
        """启用/禁用节点。

        **禁用必须发 access_revoked。** `authenticate` 虽然过滤 enabled=1,但 WS 握手
        只验一次 argon2、之后整条连接吃缓存 principal —— 不发事件的话,禁用一个节点不会
        影响它**已经建立**的连接,该连接将无限期保持授权。
        """
        events: tuple[tuple[str, str], ...] = ()
        if not enabled:
            events = ((node_id, SYNC_EVENT_ACCESS_REVOKED),)
        return self._update_fields(node_id, {"enabled": 1 if enabled else 0}, events=events)

    def set_addresses(
        self,
        node_id: str,
        *,
        endpoint_host: str | None | _Unset = UNSET,
        own_ipv6: str | None | _Unset = UNSET,
        router_id: str | None | _Unset = UNSET,
    ) -> ManagedNode:
        """局部更新地址列。UNSET = 不改动;None = 清除(交还节点本地管理)。"""
        fields = _address_fields(endpoint_host=endpoint_host, own_ipv6=own_ipv6, router_id=router_id)
        if not fields:
            node = self.get(node_id)
            if node is None:
                raise DatabaseError(f"节点不存在: {node_id}")
            return node
        # own_ipv6 / router_id 进 desired state,改了就得推给该节点。endpoint_host 不下发,
        # 但它会经由传播改到别的节点行上 —— 那些事件由 apply_address_update 负责。
        events: tuple[tuple[str, str], ...] = ()
        if "own_ipv6" in fields or "router_id" in fields:
            events = ((node_id, SYNC_EVENT_DESIRED),)
        return self._update_fields(node_id, fields, events=events)

    def apply_address_update(
        self,
        node_id: str,
        *,
        name: str | _Unset = UNSET,
        enabled: bool | _Unset = UNSET,
        endpoint_host: str | None | _Unset = UNSET,
        own_ipv6: str | None | _Unset = UNSET,
        router_id: str | None | _Unset = UNSET,
        changes: Sequence[PropagatedChange] = (),
    ) -> ManagedNode:
        """在**一个事务**里写完 managed_nodes 的字段与所有被传播的 ibgp_peers 行。

        事件:被改地址的节点发 desired(它的 config.toml / bird.conf 要重渲),每个被传播到
        的节点各发一条 desired(它的 peer 行变了),去重。禁用则额外发 access_revoked。

        传播规则见 docs/architecture/node_addressing.md。
        """
        fields = _address_fields(endpoint_host=endpoint_host, own_ipv6=own_ipv6, router_id=router_id)
        if not isinstance(name, _Unset):
            fields["name"] = name
        if not isinstance(enabled, _Unset):
            fields["enabled"] = 1 if enabled else 0

        events: list[tuple[str, str]] = []
        if "own_ipv6" in fields or "router_id" in fields:
            events.append((node_id, SYNC_EVENT_DESIRED))
        seen = {node_id}
        for change in changes:
            if change.node_id not in seen:
                seen.add(change.node_id)
                events.append((change.node_id, SYNC_EVENT_DESIRED))
        if not isinstance(enabled, _Unset) and not enabled:
            events.append((node_id, SYNC_EVENT_ACCESS_REVOKED))

        return self._update_fields(node_id, fields, events=tuple(events), changes=changes)

    def _update_fields(
        self,
        node_id: str,
        fields: dict[str, object],
        *,
        events: tuple[tuple[str, str], ...],
        changes: Sequence[PropagatedChange] = (),
    ) -> ManagedNode:
        now = _now_iso()
        # 列名全部来自本模块的字面量集合,不含外部输入。
        unknown = set(fields) - _UPDATABLE_NODE_COLUMNS
        if unknown:  # pragma: no cover — 防御性,调用方都是本模块内的字面量
            raise DatabaseError(f"不可更新的列: {sorted(unknown)}")
        assignments = ", ".join(f"{col}=?" for col in fields)
        try:
            # fields 可能为空(只有传播、节点自身字段没变)。此时仍然 UPDATE updated_at,
            # 一来保持"节点存在"的 rowcount 检查有效,二来避免拼出 "SET , updated_at=?"。
            set_clause = f"{assignments}, updated_at=?" if assignments else "updated_at=?"
            cur = self._conn.execute(
                f"UPDATE managed_nodes SET {set_clause} WHERE node_id=?",  # noqa: S608
                (*fields.values(), now, node_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise DatabaseError(f"节点不存在: {node_id}")
            for change in changes:
                if change.field not in _PROPAGATABLE_PEER_COLUMNS:  # pragma: no cover — 防御性
                    self._conn.rollback()
                    raise DatabaseError(f"不可传播的列: {change.field}")
                self._conn.execute(
                    f"UPDATE ibgp_peers SET {change.field}=?, updated_at=? WHERE node_id=? AND name=?",  # noqa: S608
                    (change.new, now, change.node_id, change.name),
                )
            for target, kind in events:
                emit_sync_event(self._conn, node_id=target, kind=kind)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 managed_node 失败") from exc
        node = self.get(node_id)
        if node is None:  # pragma: no cover — 上面已检查 rowcount
            raise DatabaseError("更新后无法读取 managed_node")
        return node


# --- config_proposals ---


VALID_PROPOSAL_SOURCES = frozenset({"push", "scan"})
VALID_PROPOSAL_KINDS = frozenset({"peer_add", "peer_modify", "peer_delete"})
VALID_PROPOSAL_STATUSES = frozenset({"pending", "accepted", "rejected"})


@dataclass(frozen=True)
class ConfigProposal:
    id: int
    node_id: str
    source: str
    kind: str
    payload: dict
    status: str
    received_at: str
    decided_at: str | None
    message: str | None


def _row_to_proposal(row: sqlite3.Row) -> ConfigProposal:
    return ConfigProposal(
        id=row["id"],
        node_id=row["node_id"],
        source=row["source"],
        kind=row["kind"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        received_at=row["received_at"],
        decided_at=row["decided_at"],
        message=row["message"],
    )


class ProposalStore:
    """CRUD for config_proposals."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(
        self,
        *,
        node_id: str,
        source: str,
        kind: str,
        payload: dict,
    ) -> ConfigProposal:
        if source not in VALID_PROPOSAL_SOURCES:
            raise DatabaseError(f"非法 source: {source}")
        if kind not in VALID_PROPOSAL_KINDS:
            raise DatabaseError(f"非法 kind: {kind}")
        now = _now_iso()
        try:
            cur = self._conn.execute(
                """
                INSERT INTO config_proposals(node_id, source, kind, payload_json, status, received_at)
                VALUES (?,?,?,?,'pending',?)
                """,
                (node_id, source, kind, json.dumps(payload, ensure_ascii=False), now),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("插入 config_proposal 失败") from exc
        row_id = cur.lastrowid
        if row_id is None:  # pragma: no cover
            raise DatabaseError("插入 proposal 后未拿到 id")
        proposal = self.get(row_id)
        if proposal is None:  # pragma: no cover
            raise DatabaseError("插入后无法读取 proposal")
        return proposal

    def get(self, proposal_id: int) -> ConfigProposal | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM config_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 proposal 失败") from exc
        return None if row is None else _row_to_proposal(row)

    def list_for_node(
        self,
        node_id: str,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[ConfigProposal]:
        params: list[object] = [node_id]
        where = "node_id=?"
        if status is not None:
            if status not in VALID_PROPOSAL_STATUSES:
                raise DatabaseError(f"非法 status 过滤: {status}")
            where += " AND status=?"
            params.append(status)
        params.append(limit)
        try:
            rows = self._conn.execute(
                f"SELECT * FROM config_proposals WHERE {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                tuple(params),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("列出 proposals 失败") from exc
        return [_row_to_proposal(r) for r in rows]

    def set_status(
        self,
        proposal_id: int,
        status: str,
        *,
        message: str | None = None,
    ) -> ConfigProposal:
        if status not in VALID_PROPOSAL_STATUSES:
            raise DatabaseError(f"非法 status: {status}")
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE config_proposals SET status=?, decided_at=?, message=? WHERE id=?",
                (status, now, message, proposal_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise DatabaseError(f"proposal 不存在: {proposal_id}")
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 proposal 状态失败") from exc
        proposal = self.get(proposal_id)
        if proposal is None:  # pragma: no cover
            raise DatabaseError("更新后无法读取 proposal")
        return proposal


# --- node_reports ---


VALID_REPORT_KINDS = frozenset({"apply_result", "scan_result", "live_status", "error"})


@dataclass(frozen=True)
class NodeReport:
    id: int
    node_id: str
    kind: str
    payload: dict
    received_at: str
    imported_at: str | None


def _row_to_report(row: sqlite3.Row) -> NodeReport:
    return NodeReport(
        id=row["id"],
        node_id=row["node_id"],
        kind=row["kind"],
        payload=json.loads(row["payload_json"]),
        received_at=row["received_at"],
        imported_at=row["imported_at"],
    )


class ReportStore:
    """CRUD for node_reports."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, *, node_id: str, kind: str, payload: dict) -> NodeReport:
        if kind not in VALID_REPORT_KINDS:
            raise DatabaseError(f"非法 report kind: {kind}")
        now = _now_iso()
        try:
            cur = self._conn.execute(
                """
                INSERT INTO node_reports(node_id, kind, payload_json, received_at)
                VALUES (?,?,?,?)
                """,
                (node_id, kind, json.dumps(payload, ensure_ascii=False), now),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("插入 node_report 失败") from exc
        row_id = cur.lastrowid
        if row_id is None:  # pragma: no cover
            raise DatabaseError("插入 report 后未拿到 id")
        report = self.get(row_id)
        if report is None:  # pragma: no cover
            raise DatabaseError("插入后无法读取 report")
        return report

    def get(self, report_id: int) -> NodeReport | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM node_reports WHERE id=?",
                (report_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 report 失败") from exc
        return None if row is None else _row_to_report(row)

    def list_for_node(
        self,
        node_id: str,
        *,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[NodeReport]:
        params: list[object] = [node_id]
        where = "node_id=?"
        if kind is not None:
            if kind not in VALID_REPORT_KINDS:
                raise DatabaseError(f"非法 kind 过滤: {kind}")
            where += " AND kind=?"
            params.append(kind)
        params.append(limit)
        try:
            rows = self._conn.execute(
                f"SELECT * FROM node_reports WHERE {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                tuple(params),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("列出 reports 失败") from exc
        return [_row_to_report(r) for r in rows]

    def mark_imported(self, report_id: int) -> NodeReport:
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE node_reports SET imported_at=? WHERE id=?",
                (now, report_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise DatabaseError(f"report 不存在: {report_id}")
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 report imported_at 失败") from exc
        report = self.get(report_id)
        if report is None:  # pragma: no cover
            raise DatabaseError("更新后无法读取 report")
        return report


# --- config_revisions ---


@dataclass(frozen=True)
class ConfigRevision:
    id: int
    node_id: str
    revision: str
    generated_at: str
    payload: dict


def _row_to_revision(row: sqlite3.Row) -> ConfigRevision:
    return ConfigRevision(
        id=row["id"],
        node_id=row["node_id"],
        revision=row["revision"],
        generated_at=row["generated_at"],
        payload=json.loads(row["payload_json"]),
    )


class RevisionStore:
    """CRUD for config_revisions plus node_desired_pin (rollback target)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        *,
        node_id: str,
        revision: str,
        generated_at: str,
        payload: dict,
    ) -> ConfigRevision:
        """Insert a revision snapshot. If the (node_id, revision) pair already
        exists (i.e. desired-state hash unchanged since last build), return the
        existing row without creating a duplicate.
        """
        existing = self.get_by_revision(node_id, revision)
        if existing is not None:
            return existing
        try:
            cur = self._conn.execute(
                """
                INSERT INTO config_revisions(node_id, revision, generated_at, payload_json)
                VALUES (?,?,?,?)
                """,
                (node_id, revision, generated_at, json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("插入 config_revision 失败") from exc
        row_id = cur.lastrowid
        if row_id is None:  # pragma: no cover
            raise DatabaseError("插入 revision 后未拿到 id")
        rev = self.get(row_id)
        if rev is None:  # pragma: no cover
            raise DatabaseError("插入后无法读取 revision")
        return rev

    def get(self, revision_id: int) -> ConfigRevision | None:
        try:
            row = self._conn.execute("SELECT * FROM config_revisions WHERE id=?", (revision_id,)).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 revision 失败") from exc
        return None if row is None else _row_to_revision(row)

    def get_by_revision(self, node_id: str, revision: str) -> ConfigRevision | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM config_revisions WHERE node_id=? AND revision=?",
                (node_id, revision),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 revision 失败") from exc
        return None if row is None else _row_to_revision(row)

    def list_for_node(self, node_id: str, *, limit: int = 50) -> list[ConfigRevision]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM config_revisions WHERE node_id=? ORDER BY id DESC LIMIT ?",
                (node_id, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("列出 revisions 失败") from exc
        return [_row_to_revision(r) for r in rows]

    def latest_revision(self, node_id: str) -> str | None:
        """Most recently recorded revision string, or None if the node has none.

        Callers use this to skip recording a snapshot whose content is identical
        to the previous one — see `services.desired_state.build_desired_state`.
        """
        try:
            row = self._conn.execute(
                "SELECT revision FROM config_revisions WHERE node_id=? ORDER BY id DESC LIMIT 1",
                (node_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询最新 revision 失败") from exc
        return None if row is None else str(row["revision"])

    def trim(self, node_id: str, *, keep_latest: int = 50) -> int:
        """Delete all but the most recent `keep_latest` revisions for `node_id`.

        The currently pinned revision (if any) is always preserved, even if it
        falls outside the recency window. Otherwise enough builds would silently
        evict the rollback target.

        Returns the number of rows deleted.
        """
        try:
            cur = self._conn.execute(
                """
                DELETE FROM config_revisions
                WHERE node_id=?
                  AND id NOT IN (
                    SELECT id FROM config_revisions WHERE node_id=?
                    ORDER BY id DESC LIMIT ?
                  )
                  AND revision NOT IN (
                    SELECT revision FROM node_desired_pin WHERE node_id=?
                  )
                """,
                (node_id, node_id, keep_latest, node_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("trim revisions 失败") from exc
        return cur.rowcount

    def pin(self, node_id: str, revision: str) -> None:
        """Mark `revision` as the desired revision for `node_id`. The revision
        must exist in config_revisions.
        """
        existing = self.get_by_revision(node_id, revision)
        if existing is None:
            raise DatabaseError(f"revision 不存在: node={node_id} revision={revision}")
        now = _now_iso()
        try:
            self._conn.execute(
                """
                INSERT INTO node_desired_pin(node_id, revision, pinned_at) VALUES (?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                    revision=excluded.revision,
                    pinned_at=excluded.pinned_at
                """,
                (node_id, revision, now),
            )
            # rollback 不碰 peer 表,但确实改变了该节点的 desired state。
            emit_sync_event(self._conn, node_id=node_id, kind=SYNC_EVENT_DESIRED)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("pin revision 失败") from exc

    def unpin(self, node_id: str) -> None:
        try:
            self._conn.execute("DELETE FROM node_desired_pin WHERE node_id=?", (node_id,))
            emit_sync_event(self._conn, node_id=node_id, kind=SYNC_EVENT_DESIRED)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("unpin revision 失败") from exc

    def get_pin(self, node_id: str) -> ConfigRevision | None:
        """Return the pinned revision row, or None if no pin (i.e. follow latest)."""
        try:
            pin_row = self._conn.execute("SELECT revision FROM node_desired_pin WHERE node_id=?", (node_id,)).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 pin 失败") from exc
        if pin_row is None:
            return None
        return self.get_by_revision(node_id, pin_row["revision"])


# --- sync_events ---


@dataclass(frozen=True)
class SyncEvent:
    id: int
    node_id: str
    kind: str
    created_at: str


def _row_to_sync_event(row: sqlite3.Row) -> SyncEvent:
    return SyncEvent(
        id=row["id"],
        node_id=row["node_id"],
        kind=row["kind"],
        created_at=row["created_at"],
    )


class SyncEventStore:
    """Read side of the change-notification queue.

    The write side lives in `dn42ctl.db.emit_sync_event` — it has to, because
    `db_managed` imports `db` and the emit has to run inside each mutation's own
    transaction. Only `dn42ctl serve`'s watcher reads this table.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def latest_id(self) -> int:
        """Current high-water mark. The watcher starts here rather than at 0:
        anything older is already covered by each connection's initial sync.
        """
        try:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM sync_events").fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 sync_events 游标失败") from exc
        return int(row[0])

    def fetch_since(self, last_id: int, *, limit: int = 500) -> list[SyncEvent]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM sync_events WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError("查询 sync_events 失败") from exc
        return [_row_to_sync_event(r) for r in rows]
