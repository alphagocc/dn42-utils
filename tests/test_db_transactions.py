"""事务残留 + 锁的回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dn42ctl.db import Database, DatabaseError
from dn42ctl.db_managed import ManagedNodeStore, ProposalStore, ReportStore, hash_token

NODE_A = "11111111-1111-4111-8111-111111111111"


class TestNoLingeringWriteTransaction:
    """自定义异常绕过 except sqlite3.Error,不显式 rollback 就会留下写事务。

    0 行匹配的 UPDATE 照样拿 RESERVED 锁,另一个连接会先卡满 busy_timeout 再报
    database is locked。见 docs/architecture/database.md 的"事务纪律"。
    """

    def test_update_bgp_peer_missing(self, mem_db: Database) -> None:
        mem_db.ensure_node(NODE_A)
        with pytest.raises(DatabaseError, match="not found"):
            mem_db.update_bgp_peer(
                node_id=NODE_A,
                peer_asn=1234,
                peer_public_key="k",
                endpoint="",
                peer_lla="fe80::1",
                listen_port=1234,
                allowed_ips=["fe80::/64"],
                net_backend="networkd",
            )
        assert mem_db.connection.in_transaction is False

    def test_update_ibgp_peer_missing(self, mem_db: Database) -> None:
        mem_db.ensure_node(NODE_A)
        with pytest.raises(DatabaseError, match="not found"):
            mem_db.update_ibgp_peer(
                node_id=NODE_A,
                name="nope",
                peer_public_key="k",
                endpoint="",
                peer_lla="fe80::1",
                listen_port=1234,
                allowed_ips=["fe80::/64"],
                net_backend="networkd",
                babel_rxcost=20,
                peer_ip="fd42::1",
                babel_type="tunnel",
            )
        assert mem_db.connection.in_transaction is False

    @pytest.mark.parametrize(
        "action",
        [
            lambda db: ManagedNodeStore(db.connection).set_token_hash(NODE_A, hash_token("x")),
            lambda db: ManagedNodeStore(db.connection).set_write_policy(NODE_A, {}),
            lambda db: ManagedNodeStore(db.connection).set_name(NODE_A, "x"),
            lambda db: ManagedNodeStore(db.connection).set_enabled(NODE_A, False),
            lambda db: ManagedNodeStore(db.connection).set_addresses(NODE_A, own_ipv6="fd42::9"),
            lambda db: ProposalStore(db.connection).set_status(999, "accepted"),
            lambda db: ReportStore(db.connection).mark_imported(999),
        ],
    )
    def test_missing_row_leaves_no_open_txn(self, mem_db: Database, action) -> None:  # noqa: ANN001
        with pytest.raises(DatabaseError):
            action(mem_db)
        assert mem_db.connection.in_transaction is False

    def test_second_connection_can_still_write(self, tmp_path: Path) -> None:
        """真正要防的后果:另一个连接被这把锁挡住。"""
        db_path = tmp_path / "lock.sqlite3"
        db = Database.open(db_path)
        try:
            db.ensure_node(NODE_A)
            with pytest.raises(DatabaseError):
                ManagedNodeStore(db.connection).set_name("missing-node", "x")

            other = sqlite3.connect(db_path, timeout=1.0)
            try:
                other.execute("INSERT INTO nodes(node_id, created_at, updated_at) VALUES ('n2','x','x')")
                other.commit()
            finally:
                other.close()
        finally:
            db.close()
