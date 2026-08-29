from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dn42ctl.constants import (
    FILE_MODE_PRIVATE,
    SQLITE_BUSY_TIMEOUT_MS,
    SYNC_EVENT_DESIRED,
    SYNC_EVENTS_KEEP,
    SYNC_EVENTS_TRIM_EVERY,
    UNSET,
    _Unset,
)
from dn42ctl.fs import chmod_best_effort
from dn42ctl.migrations import MIGRATIONS


class DatabaseError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def emit_sync_event(conn: sqlite3.Connection, *, node_id: str, kind: str = SYNC_EVENT_DESIRED) -> None:
    """Append a change notification for the `dn42ctl serve` watcher.

    Inserts into the CALLER'S already-open transaction — the caller commits. That
    makes the event atomic with the business write it describes: there is no crash
    window where the peer row landed but the notification did not.

    `kind` is one of `SYNC_EVENT_DESIRED` (the node's desired state may have changed)
    or `SYNC_EVENT_ACCESS_REVOKED` (drop this node's live connections).

    Trimming is amortized: every `SYNC_EVENTS_TRIM_EVERY` rows we drop everything
    older than the newest `SYNC_EVENTS_KEEP`. Safe at any cursor position because the
    watcher initializes its cursor to `MAX(id)` and only ever moves forward.
    """
    cur = conn.execute(
        "INSERT INTO sync_events(node_id, kind, created_at) VALUES (?,?,?)",
        (node_id, kind, _now_iso()),
    )
    new_id = cur.lastrowid
    if new_id is not None and new_id % SYNC_EVENTS_TRIM_EVERY == 0:
        conn.execute("DELETE FROM sync_events WHERE id <= ?", (new_id - SYNC_EVENTS_KEEP,))


@dataclass(frozen=True)
class _PeerRecordBase:
    node_id: str
    ifname: str
    wg_private_key: str
    wg_public_key: str
    peer_public_key: str | None
    endpoint: str | None
    local_lla: str
    peer_lla: str | None
    listen_port: int
    allowed_ips: list[str]
    net_backend: str


@dataclass(frozen=True)
class BgpPeerRecord(_PeerRecordBase):
    peer_asn: int


@dataclass(frozen=True)
class IbgpPeerRecord(_PeerRecordBase):
    name: str
    babel_rxcost: int
    peer_ip: str | None = None
    has_wg: bool = True
    babel_type: str = "tunnel"
    # 这条 peer 记录所代表的受管节点。用于把节点地址变更传播到 mesh;
    # None 表示未关联,传播看不见这行。见 docs/architecture/node_addressing.md。
    remote_node_id: str | None = None


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # hub 上 server 进程与 CLI 进程并发访问同一个库文件;没有 busy_timeout 时撞锁会
        # 立刻抛 "database is locked",放弃等待重试。不启用 WAL(见 docs/architecture/database.md)。
        self._conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @classmethod
    def open(cls, db_path: Path) -> Database:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to open database: {db_path}") from exc

        db = cls(conn)
        db.migrate()

        # DB may store WireGuard private keys; try to restrict permissions.
        chmod_best_effort(db_path, FILE_MODE_PRIVATE)
        return db

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        applied = {
            int(row[0])
            for row in self._conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        }

        for version, step in MIGRATIONS:
            if version in applied:
                continue
            try:
                if callable(step):
                    step(self._conn)
                else:
                    self._conn.executescript(step)
                self._conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise DatabaseError(f"Migration failed at version {version}") from exc

    def ensure_node(self, node_id: str) -> None:
        now = _now_iso()
        try:
            self._conn.execute(
                """
                INSERT INTO nodes(node_id, created_at, updated_at) VALUES (?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET updated_at=excluded.updated_at
                """.strip(),
                (node_id, now, now),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to upsert node") from exc

    def get_bgp_peer(self, node_id: str, peer_asn: int) -> sqlite3.Row | None:
        try:
            return self._conn.execute(
                "SELECT * FROM bgp_peers WHERE node_id=? AND peer_asn=?",
                (node_id, peer_asn),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("Failed to query BGP peer") from exc

    def list_bgp_peers(self, node_id: str) -> list[sqlite3.Row]:
        try:
            return list(
                self._conn.execute(
                    "SELECT * FROM bgp_peers WHERE node_id=? ORDER BY peer_asn",
                    (node_id,),
                ).fetchall()
            )
        except sqlite3.Error as exc:
            raise DatabaseError("Failed to list BGP peers") from exc

    def insert_bgp_peer(self, record: BgpPeerRecord) -> None:
        now = _now_iso()
        try:
            self._conn.execute(
                """
                INSERT INTO bgp_peers(
                    node_id, peer_asn, ifname,
                    wg_private_key, wg_public_key,
                    peer_public_key, endpoint,
                    local_lla, peer_lla, listen_port,
                    allowed_ips_json, net_backend,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """.strip(),
                (
                    record.node_id,
                    record.peer_asn,
                    record.ifname,
                    record.wg_private_key,
                    record.wg_public_key,
                    record.peer_public_key,
                    record.endpoint,
                    record.local_lla,
                    record.peer_lla,
                    record.listen_port,
                    json.dumps(record.allowed_ips, ensure_ascii=False),
                    record.net_backend,
                    now,
                    now,
                ),
            )
            emit_sync_event(self._conn, node_id=record.node_id)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DatabaseError("BGP peer already exists") from exc
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to insert BGP peer") from exc

    def update_bgp_peer(
        self,
        *,
        node_id: str,
        peer_asn: int,
        peer_public_key: str | None,
        endpoint: str | None,
        peer_lla: str | None,
        listen_port: int,
        allowed_ips: list[str],
        net_backend: str,
    ) -> None:
        now = _now_iso()
        try:
            cur = self._conn.execute(
                """
                UPDATE bgp_peers
                SET peer_public_key=?, endpoint=?, peer_lla=?,
                    listen_port=?,
                    allowed_ips_json=?, net_backend=?, updated_at=?
                WHERE node_id=? AND peer_asn=?
                """.strip(),
                (
                    peer_public_key,
                    endpoint,
                    peer_lla,
                    listen_port,
                    json.dumps(allowed_ips, ensure_ascii=False),
                    net_backend,
                    now,
                    node_id,
                    peer_asn,
                ),
            )
            if cur.rowcount == 0:
                # SQLite may report 0 if values are unchanged; disambiguate by existence.
                exists = self._conn.execute(
                    "SELECT 1 FROM bgp_peers WHERE node_id=? AND peer_asn=?",
                    (node_id, peer_asn),
                ).fetchone()
                if exists is None:
                    # 自定义异常绕过下面的 except sqlite3.Error,不显式回滚就会留下写事务。
                    self._conn.rollback()
                    raise DatabaseError("BGP peer not found")
            emit_sync_event(self._conn, node_id=node_id)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to update BGP peer") from exc

    def list_ibgp_peers(self, node_id: str) -> list[sqlite3.Row]:
        try:
            return list(
                self._conn.execute(
                    "SELECT * FROM ibgp_peers WHERE node_id=? ORDER BY name",
                    (node_id,),
                ).fetchall()
            )
        except sqlite3.Error as exc:
            raise DatabaseError("Failed to list iBGP peers") from exc

    def list_ibgp_peers_by_remote(self, remote_node_id: str) -> list[sqlite3.Row]:
        """所有指向该受管节点的 iBGP peer 行,**跨全部 node_id 分区**。

        这些行属于其他节点(它们是"从 B 看向 A"的记录),节点地址传播要改写的正是它们。
        """
        try:
            return list(
                self._conn.execute(
                    "SELECT * FROM ibgp_peers WHERE remote_node_id=? ORDER BY node_id, name",
                    (remote_node_id,),
                ).fetchall()
            )
        except sqlite3.Error as exc:
            raise DatabaseError("Failed to list iBGP peers by remote node") from exc

    def get_ibgp_peer(self, node_id: str, name: str) -> sqlite3.Row | None:
        try:
            return self._conn.execute(
                "SELECT * FROM ibgp_peers WHERE node_id=? AND name=?",
                (node_id, name),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("Failed to query iBGP peer") from exc

    def _delete_peer(
        self,
        table: str,
        where_clause: str,
        get_fn: Callable[..., sqlite3.Row | None],
        where_params: tuple,
        error_label: str,
        node_id: str,
    ) -> sqlite3.Row | None:
        try:
            row = get_fn(*where_params)
            if row is None:
                return None
            self._conn.execute(
                f"DELETE FROM {table} WHERE {where_clause}",  # noqa: S608
                where_params,
            )
            emit_sync_event(self._conn, node_id=node_id)
            self._conn.commit()
            return row
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError(f"Failed to delete {error_label}") from exc

    def delete_bgp_peer(self, node_id: str, peer_asn: int) -> sqlite3.Row | None:
        return self._delete_peer(
            "bgp_peers",
            "node_id=? AND peer_asn=?",
            self.get_bgp_peer,
            (node_id, peer_asn),
            "BGP peer",
            node_id,
        )

    def delete_ibgp_peer(self, node_id: str, name: str) -> sqlite3.Row | None:
        return self._delete_peer(
            "ibgp_peers",
            "node_id=? AND name=?",
            self.get_ibgp_peer,
            (node_id, name),
            "iBGP peer",
            node_id,
        )

    def insert_ibgp_peer(self, record: IbgpPeerRecord) -> None:
        now = _now_iso()
        try:
            self._conn.execute(
                """
                INSERT INTO ibgp_peers(
                    node_id, name, ifname,
                    wg_private_key, wg_public_key,
                    peer_public_key, endpoint,
                    local_lla, peer_lla, listen_port,
                    allowed_ips_json, net_backend,
                    babel_rxcost, peer_ip, has_wg,
                    babel_type, remote_node_id,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """.strip(),
                (
                    record.node_id,
                    record.name,
                    record.ifname,
                    record.wg_private_key,
                    record.wg_public_key,
                    record.peer_public_key,
                    record.endpoint,
                    record.local_lla,
                    record.peer_lla,
                    record.listen_port,
                    json.dumps(record.allowed_ips, ensure_ascii=False),
                    record.net_backend,
                    record.babel_rxcost,
                    record.peer_ip,
                    1 if record.has_wg else 0,
                    record.babel_type,
                    record.remote_node_id,
                    now,
                    now,
                ),
            )
            emit_sync_event(self._conn, node_id=record.node_id)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DatabaseError("iBGP peer already exists") from exc
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to insert iBGP peer") from exc

    def update_ibgp_peer(
        self,
        *,
        node_id: str,
        name: str,
        peer_public_key: str | None,
        endpoint: str | None,
        peer_lla: str | None,
        listen_port: int,
        allowed_ips: list[str],
        net_backend: str,
        babel_rxcost: int,
        peer_ip: str | None,
        babel_type: str,
        remote_node_id: str | None | _Unset = UNSET,
    ) -> None:
        now = _now_iso()
        # remote_node_id 默认 UNSET,整列不进 SET 子句。调用方(proposal 接受、
        # 上报导入)并不知道这个关联的存在,不能让它们静默把链接抹掉。
        extra_set = ""
        extra_params: tuple[object, ...] = ()
        if not isinstance(remote_node_id, _Unset):
            extra_set = " remote_node_id=?,"
            extra_params = (remote_node_id,)
        try:
            cur = self._conn.execute(
                f"""
                UPDATE ibgp_peers
                SET peer_public_key=?, endpoint=?, peer_lla=?,
                    listen_port=?, allowed_ips_json=?, net_backend=?,
                    babel_rxcost=?, peer_ip=?, babel_type=?,{extra_set} updated_at=?
                WHERE node_id=? AND name=?
                """.strip(),  # noqa: S608 — extra_set 是本方法的字面量,不含外部输入
                (
                    peer_public_key,
                    endpoint,
                    peer_lla,
                    listen_port,
                    json.dumps(allowed_ips, ensure_ascii=False),
                    net_backend,
                    babel_rxcost,
                    peer_ip,
                    babel_type,
                    *extra_params,
                    now,
                    node_id,
                    name,
                ),
            )
            if cur.rowcount == 0:
                exists = self._conn.execute(
                    "SELECT 1 FROM ibgp_peers WHERE node_id=? AND name=?",
                    (node_id, name),
                ).fetchone()
                if exists is None:
                    self._conn.rollback()
                    raise DatabaseError("iBGP peer not found")
            emit_sync_event(self._conn, node_id=node_id)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to update iBGP peer") from exc

    def get_used_listen_ports(self, node_id: str) -> set[int]:
        ports: set[int] = set()
        try:
            for row in self._conn.execute(
                "SELECT listen_port FROM bgp_peers WHERE node_id=?",
                (node_id,),
            ).fetchall():
                ports.add(int(row[0]))
            for row in self._conn.execute(
                "SELECT listen_port FROM ibgp_peers WHERE node_id=?",
                (node_id,),
            ).fetchall():
                ports.add(int(row[0]))
        except sqlite3.Error as exc:
            raise DatabaseError("Failed to query listen ports") from exc
        return ports
