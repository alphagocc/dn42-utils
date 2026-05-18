from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from dn42ctl.db import DatabaseError


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
    )


# argon2id with library defaults (memory_cost=64 MiB, time_cost=3, parallelism=4).
# For tests, callers can swap _password_hasher with a faster instance if needed.
_password_hasher = PasswordHasher()


def hash_token(token: str) -> str:
    return _password_hasher.hash(token)


def verify_token(stored_hash: str, token: str) -> bool:
    try:
        return _password_hasher.verify(stored_hash, token)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 — any hash parse error means invalid
        return False


class ManagedNodeStore:
    """CRUD for the managed_nodes table.

    Receives an already-open sqlite3.Connection (typically from Database.connection)
    so that callers can share transactions with other stores.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- create / read ----

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
            row = self._conn.execute(
                "SELECT * FROM managed_nodes WHERE is_self=1 LIMIT 1",
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
        """
        now = _now_iso()
        try:
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

    # ---- delete ----

    def delete(self, node_id: str, *, force: bool = False) -> ManagedNode | None:
        existing = self.get(node_id)
        if existing is None:
            return None
        if existing.is_self and not force:
            raise DatabaseError("拒绝删除 self 节点 (传入 force=True 强制删除)")
        try:
            self._conn.execute("DELETE FROM managed_nodes WHERE node_id=?", (node_id,))
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("删除 managed_node 失败") from exc
        return existing

    # ---- token signing / verification ----

    def set_token_hash(self, node_id: str, token_hash: str) -> None:
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE managed_nodes SET api_token_hash=?, updated_at=? WHERE node_id=?",
                (token_hash, now, node_id),
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"节点不存在: {node_id}")
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("更新 token hash 失败") from exc

    def rotate_token(self, node_id: str, new_plaintext: str) -> None:
        """Hash new_plaintext and store it; idempotent across retries."""
        self.set_token_hash(node_id, hash_token(new_plaintext))

    def authenticate(self, token: str) -> ManagedNode | None:
        """Look up a node by Bearer token.

        Walks all enabled nodes with a non-null hash; argon2 verify is constant-time
        per row. The number of nodes is small (<100 in practice), so a linear scan
        is fine and avoids leaking timing about which node_id is registered.
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

    # ---- policy ----

    def set_write_policy(self, node_id: str, policy: dict[str, str]) -> ManagedNode:
        normalized = validate_write_policy(policy)
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE managed_nodes SET write_policy=?, updated_at=? WHERE node_id=?",
                (json.dumps(normalized, ensure_ascii=False), now, node_id),
            )
            if cur.rowcount == 0:
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
