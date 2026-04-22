from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dn42ctl.constants import FILE_MODE_PRIVATE
from dn42ctl.fs import chmod_best_effort
from dn42ctl.migrations import MIGRATIONS


class DatabaseError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def open(cls, db_path: Path) -> "Database":
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
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
        )
        applied = {
            int(row[0])
            for row in self._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        }

        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            try:
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
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
                    raise DatabaseError("BGP peer not found")
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
        get_fn: callable,
        where_params: tuple,
        error_label: str,
    ) -> sqlite3.Row | None:
        try:
            row = get_fn(*where_params)
            if row is None:
                return None
            self._conn.execute(
                f"DELETE FROM {table} WHERE {where_clause}",  # noqa: S608
                where_params,
            )
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
        )

    def delete_ibgp_peer(self, node_id: str, name: str) -> sqlite3.Row | None:
        return self._delete_peer(
            "ibgp_peers",
            "node_id=? AND name=?",
            self.get_ibgp_peer,
            (node_id, name),
            "iBGP peer",
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
                    babel_rxcost,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DatabaseError("iBGP peer already exists") from exc
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to insert iBGP peer") from exc

    def update_ibgp_peer_rxcost(
        self,
        *,
        node_id: str,
        name: str,
        babel_rxcost: int,
    ) -> None:
        now = _now_iso()
        try:
            cur = self._conn.execute(
                """
                UPDATE ibgp_peers
                SET babel_rxcost=?, updated_at=?
                WHERE node_id=? AND name=?
                """.strip(),
                (babel_rxcost, now, node_id, name),
            )
            if cur.rowcount == 0:
                exists = self._conn.execute(
                    "SELECT 1 FROM ibgp_peers WHERE node_id=? AND name=?",
                    (node_id, name),
                ).fetchone()
                if exists is None:
                    raise DatabaseError("iBGP peer not found")
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise DatabaseError("Failed to update iBGP peer rxcost") from exc

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
