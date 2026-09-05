from __future__ import annotations

import sqlite3

import pytest

from dn42ctl.constants import (
    SYNC_EVENT_ACCESS_REVOKED,
    SYNC_EVENT_DESIRED,
    SYNC_EVENTS_KEEP,
    SYNC_EVENTS_TRIM_EVERY,
)
from dn42ctl.db import BgpPeerRecord, Database, DatabaseError, IbgpPeerRecord, emit_sync_event
from dn42ctl.db_managed import ManagedNodeStore, PropagatedChange, RevisionStore, SyncEventStore

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


def _bgp(node_id: str = "test-node", peer_asn: int = 4242420001) -> BgpPeerRecord:
    return BgpPeerRecord(
        node_id=node_id,
        peer_asn=peer_asn,
        ifname=f"dn42_p{peer_asn}",
        wg_private_key="priv",
        wg_public_key="pub",
        peer_public_key="peerpub",
        endpoint="example.com:51820",
        local_lla="fe80::1",
        peer_lla="fe80::2",
        listen_port=31000,
        allowed_ips=["fe80::/64", "fd00::/8"],
        net_backend="networkd",
    )


def _ibgp(node_id: str = "test-node", name: str = "site-a") -> IbgpPeerRecord:
    return IbgpPeerRecord(
        node_id=node_id,
        name=name,
        ifname=f"wg_{name}",
        wg_private_key="priv",
        wg_public_key="pub",
        peer_public_key="peerpub",
        endpoint=None,
        local_lla="fe80::1",
        peer_lla="fe80::2",
        listen_port=31001,
        allowed_ips=["::/0"],
        net_backend="networkd",
        babel_rxcost=20,
        peer_ip="fd42:4242:1234::2",
        has_wg=True,
        babel_type="tunnel",
    )


def _events(db: Database) -> list[tuple[str, str]]:
    rows = db.connection.execute("SELECT node_id, kind FROM sync_events ORDER BY id").fetchall()
    return [(r["node_id"], r["kind"]) for r in rows]


class TestMigration:
    def test_autoincrement_declared(self, mem_db: Database) -> None:
        """Plain INTEGER PRIMARY KEY would reuse rowids after a trim, silently
        rewinding the watcher cursor. The DDL must say AUTOINCREMENT.
        """
        sql = mem_db.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_events'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" in sql.upper()

    def test_no_foreign_key(self, mem_db: Database) -> None:
        """remove_node emits access_revoked; that row must outlive the node."""
        fks = mem_db.connection.execute("PRAGMA foreign_key_list(sync_events)").fetchall()
        assert fks == []


class TestPeerMutationsEmit:
    def test_insert_bgp(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_bgp_peer(_bgp())
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)]

    def test_update_bgp(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_bgp_peer(_bgp())
        mem_db_with_node.update_bgp_peer(
            node_id="test-node",
            peer_asn=4242420001,
            peer_public_key="newpub",
            endpoint=None,
            peer_lla="fe80::3",
            listen_port=31002,
            allowed_ips=["fe80::/64"],
            net_backend="networkd",
        )
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)] * 2

    def test_delete_bgp(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_bgp_peer(_bgp())
        mem_db_with_node.delete_bgp_peer("test-node", 4242420001)
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)] * 2

    def test_delete_missing_bgp_emits_nothing(self, mem_db_with_node: Database) -> None:
        assert mem_db_with_node.delete_bgp_peer("test-node", 4242429999) is None
        assert _events(mem_db_with_node) == []

    def test_insert_ibgp(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_ibgp())
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)]

    def test_update_ibgp(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_ibgp())
        mem_db_with_node.update_ibgp_peer(
            node_id="test-node",
            name="site-a",
            peer_public_key="newpub",
            endpoint=None,
            peer_lla="fe80::3",
            listen_port=31003,
            allowed_ips=["::/0"],
            net_backend="networkd",
            babel_rxcost=30,
            peer_ip="fd42:4242:1234::3",
            babel_type="tunnel",
        )
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)] * 2

    def test_delete_ibgp(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_ibgp())
        mem_db_with_node.delete_ibgp_peer("test-node", "site-a")
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)] * 2

    def test_delete_missing_ibgp_emits_nothing(self, mem_db_with_node: Database) -> None:
        assert mem_db_with_node.delete_ibgp_peer("test-node", "nope") is None
        assert _events(mem_db_with_node) == []

    def test_node_id_is_the_peers_own_node(self, mem_db: Database) -> None:
        """The event must carry the peer's node_id, not some ambient default."""
        mem_db.ensure_node(NODE_B)
        mem_db.insert_bgp_peer(_bgp(node_id=NODE_B))
        assert _events(mem_db) == [(NODE_B, SYNC_EVENT_DESIRED)]

    def test_failed_insert_rolls_back_event(self, mem_db_with_node: Database) -> None:
        """Duplicate insert must leave neither a peer row nor an event."""
        mem_db_with_node.insert_bgp_peer(_bgp())
        with pytest.raises(DatabaseError):
            mem_db_with_node.insert_bgp_peer(_bgp())
        assert _events(mem_db_with_node) == [("test-node", SYNC_EVENT_DESIRED)]


class TestManagedNodeMutationsEmit:
    def test_set_token_hash_revokes(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.rotate_token(NODE_A, "plaintext-token")
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_ACCESS_REVOKED)]

    def test_delete_revokes_and_row_survives(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.delete(NODE_A)
        # The managed_nodes row is gone but the notification must remain.
        assert store.get(NODE_A) is None
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_ACCESS_REVOKED)]

    def test_delete_missing_emits_nothing(self, mem_db: Database) -> None:
        assert ManagedNodeStore(mem_db.connection).delete(NODE_A) is None
        assert _events(mem_db) == []

    def test_pin_and_unpin_emit_desired(self, mem_db: Database) -> None:
        ManagedNodeStore(mem_db.connection).add(NODE_A, "alpha")
        store = RevisionStore(mem_db.connection)
        store.record(node_id=NODE_A, revision="r1", generated_at="t1", payload={})
        store.pin(NODE_A, "r1")
        store.unpin(NODE_A)
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_DESIRED)] * 2

    def test_failed_pin_emits_nothing(self, mem_db: Database) -> None:
        ManagedNodeStore(mem_db.connection).add(NODE_A, "alpha")
        with pytest.raises(DatabaseError):
            RevisionStore(mem_db.connection).pin(NODE_A, "no-such-revision")
        assert _events(mem_db) == []

    def test_disable_revokes_access(self, mem_db: Database) -> None:
        """禁用节点必须撤销它**已经建立**的连接。

        authenticate 过滤 enabled=1,但 WS 握手只验一次 token、之后整条连接吃缓存
        principal —— 不发事件的话被禁用的节点会无限期保持授权。
        """
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.set_enabled(NODE_A, enabled=False)
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_ACCESS_REVOKED)]

    def test_enable_emits_nothing(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.set_enabled(NODE_A, enabled=True)
        assert _events(mem_db) == []

    def test_set_name_emits_nothing(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.set_name(NODE_A, "renamed")
        assert _events(mem_db) == []


class TestNodeAddressEvents:
    """own_ipv6 / router_id 进 desired state,改了就得推;endpoint_host 不下发。"""

    def test_own_ipv6_emits_desired(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.set_addresses(NODE_A, own_ipv6="fd42:4242:1::1")
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_DESIRED)]

    def test_router_id_emits_desired(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.set_addresses(NODE_A, router_id="172.20.1.1")
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_DESIRED)]

    def test_endpoint_host_alone_emits_nothing(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.set_addresses(NODE_A, endpoint_host="a.example.com")
        assert _events(mem_db) == []

    def test_propagation_emits_one_event_per_affected_node(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        mem_db.ensure_node("test-node")
        mem_db.insert_ibgp_peer(_ibgp(node_id=NODE_B, name="to-a"))
        mem_db.insert_ibgp_peer(_ibgp(node_id="test-node", name="to-a"))
        mem_db.connection.execute("DELETE FROM sync_events")
        mem_db.connection.commit()

        node = store.apply_address_update(
            NODE_A,
            own_ipv6="fd42:4242:1::9",
            changes=[
                PropagatedChange(NODE_B, "to-a", "peer_ip", None, "fd42:4242:1::9"),
                PropagatedChange("test-node", "to-a", "peer_ip", None, "fd42:4242:1::9"),
            ],
        )
        assert node.own_ipv6 == "fd42:4242:1::9"
        # A 自己(地址块变了)+ 每个被传播到的节点各一条
        assert _events(mem_db) == [
            (NODE_A, SYNC_EVENT_DESIRED),
            (NODE_B, SYNC_EVENT_DESIRED),
            ("test-node", SYNC_EVENT_DESIRED),
        ]
        assert mem_db.get_ibgp_peer(NODE_B, "to-a")["peer_ip"] == "fd42:4242:1::9"
        assert mem_db.get_ibgp_peer("test-node", "to-a")["peer_ip"] == "fd42:4242:1::9"

    def test_propagation_dedups_self(self, mem_db: Database) -> None:
        """被编辑的节点自己也有一条指向自己的行时,不能发两条 desired。"""
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        mem_db.insert_ibgp_peer(_ibgp(node_id=NODE_A, name="loop"))
        mem_db.connection.execute("DELETE FROM sync_events")
        mem_db.connection.commit()

        store.apply_address_update(
            NODE_A,
            own_ipv6="fd42:4242:1::9",
            changes=[PropagatedChange(NODE_A, "loop", "peer_ip", None, "fd42:4242:1::9")],
        )
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_DESIRED)]

    def test_endpoint_host_only_still_emits_for_propagated_nodes(self, mem_db: Database) -> None:
        """endpoint_host 自己不下发,但被它改写的那些行属于别的节点,那些节点要收推送。"""
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        mem_db.insert_ibgp_peer(_ibgp(node_id=NODE_B, name="to-a"))
        mem_db.connection.execute("DELETE FROM sync_events")
        mem_db.connection.commit()

        store.apply_address_update(
            NODE_A,
            endpoint_host="new.example.com",
            changes=[PropagatedChange(NODE_B, "to-a", "endpoint", "old:51821", "new.example.com:51821")],
        )
        assert _events(mem_db) == [(NODE_B, SYNC_EVENT_DESIRED)]
        assert mem_db.get_ibgp_peer(NODE_B, "to-a")["endpoint"] == "new.example.com:51821"

    def test_disable_via_apply_address_update_revokes(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        store.add(NODE_A, "alpha")
        store.apply_address_update(NODE_A, enabled=False)
        assert _events(mem_db) == [(NODE_A, SYNC_EVENT_ACCESS_REVOKED)]

    def test_failed_update_emits_nothing(self, mem_db: Database) -> None:
        store = ManagedNodeStore(mem_db.connection)
        with pytest.raises(DatabaseError, match="节点不存在"):
            store.apply_address_update(NODE_A, own_ipv6="fd42:4242:1::9")
        assert _events(mem_db) == []


class TestSyncEventStore:
    def test_latest_id_empty(self, mem_db: Database) -> None:
        assert SyncEventStore(mem_db.connection).latest_id() == 0

    def test_fetch_since(self, mem_db_with_node: Database) -> None:
        store = SyncEventStore(mem_db_with_node.connection)
        mem_db_with_node.insert_bgp_peer(_bgp(peer_asn=4242420001))
        cursor = store.latest_id()
        mem_db_with_node.insert_bgp_peer(_bgp(peer_asn=4242420002))

        fresh = store.fetch_since(cursor)
        assert len(fresh) == 1
        assert fresh[0].node_id == "test-node"
        assert fresh[0].kind == SYNC_EVENT_DESIRED
        assert fresh[0].id > cursor

    def test_fetch_since_returns_ordered(self, mem_db: Database) -> None:
        for nid in (NODE_A, NODE_B, NODE_A):
            emit_sync_event(mem_db.connection, node_id=nid)
        mem_db.connection.commit()
        rows = SyncEventStore(mem_db.connection).fetch_since(0)
        assert [r.node_id for r in rows] == [NODE_A, NODE_B, NODE_A]
        assert [r.id for r in rows] == sorted(r.id for r in rows)

    def test_fetch_since_respects_limit(self, mem_db: Database) -> None:
        for _ in range(10):
            emit_sync_event(mem_db.connection, node_id=NODE_A)
        mem_db.connection.commit()
        assert len(SyncEventStore(mem_db.connection).fetch_since(0, limit=4)) == 4

    def test_fetch_since_at_high_water_is_empty(self, mem_db: Database) -> None:
        emit_sync_event(mem_db.connection, node_id=NODE_A)
        mem_db.connection.commit()
        store = SyncEventStore(mem_db.connection)
        assert store.fetch_since(store.latest_id()) == []


class TestTrimming:
    def test_bounded_growth(self, mem_db: Database) -> None:
        """Trimming is amortized, so the table settles at KEEP plus at most one
        un-trimmed batch. Asserting against the inserted count instead would pass
        even with trimming disabled entirely.
        """
        for _ in range(SYNC_EVENTS_KEEP + 600):
            emit_sync_event(mem_db.connection, node_id=NODE_A)
        mem_db.connection.commit()
        count = mem_db.connection.execute("SELECT COUNT(*) FROM sync_events").fetchone()[0]
        assert count <= SYNC_EVENTS_KEEP + SYNC_EVENTS_TRIM_EVERY

    def test_trim_does_not_strand_a_cursor_at_high_water(self, mem_db: Database) -> None:
        store = SyncEventStore(mem_db.connection)
        for _ in range(SYNC_EVENTS_KEEP + 600):
            emit_sync_event(mem_db.connection, node_id=NODE_A)
        mem_db.connection.commit()
        cursor = store.latest_id()
        emit_sync_event(mem_db.connection, node_id=NODE_B)
        mem_db.connection.commit()
        fresh = store.fetch_since(cursor)
        assert [e.node_id for e in fresh] == [NODE_B]


class TestBusyTimeout:
    def test_pragma_is_set(self, tmp_path) -> None:
        db = Database.open(tmp_path / "t.sqlite3")
        try:
            assert db.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            db.close()

    def test_journal_mode_stays_default(self, tmp_path) -> None:
        """WAL is deliberately NOT enabled — see docs/architecture/database.md."""
        db = Database.open(tmp_path / "t.sqlite3")
        try:
            mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() != "wal"
        finally:
            db.close()

    def test_memory_db_unaffected(self) -> None:
        conn = sqlite3.connect(":memory:")
        db = Database(conn)
        db.migrate()
        assert db.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
