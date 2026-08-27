from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.db import BgpPeerRecord, Database, DatabaseError, IbgpPeerRecord
from dn42ctl.migrations import MIGRATIONS


class TestMigrations:
    def test_tables_exist(self, mem_db: Database) -> None:
        conn = mem_db._conn
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "schema_migrations" in tables
        assert "nodes" in tables
        assert "bgp_peers" in tables
        assert "ibgp_peers" in tables
        # v5 hub-spoke sync tables
        assert "managed_nodes" in tables
        assert "config_proposals" in tables
        assert "node_reports" in tables
        assert "config_revisions" in tables
        # v6 rollback pin
        assert "node_desired_pin" in tables
        # v9 change-notification queue
        assert "sync_events" in tables

    def test_v10_address_columns_exist(self, mem_db: Database) -> None:
        conn = mem_db._conn
        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(managed_nodes)")}
        assert {"endpoint_host", "own_ipv6", "router_id"} <= node_cols
        ibgp_cols = {row[1] for row in conn.execute("PRAGMA table_info(ibgp_peers)")}
        assert "remote_node_id" in ibgp_cols

    def test_migrate_idempotent(self, mem_db: Database) -> None:
        mem_db.migrate()
        mem_db.migrate()
        rows = mem_db._conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [r[0] for r in rows] == [v for v, _ in MIGRATIONS]

    def test_migrate_reruns_on_partially_applied_db(self, mem_db: Database) -> None:
        """v10 的列已存在但版本号缺失时,重跑不能因 duplicate column 而炸。

        这正是 ALTER TABLE 必须走 callable 分支的理由:executescript 会隐式提交,一旦
        脚本中途失败就会留下"列已加、版本号没写"的状态,裸 ALTER 重跑会把库永久卡死。
        """
        mem_db._conn.execute("DELETE FROM schema_migrations WHERE version=10")
        mem_db._conn.commit()
        mem_db.migrate()  # 不应抛异常
        versions = [r[0] for r in mem_db._conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [v for v, _ in MIGRATIONS]

    def test_v2_converts_nm_to_networkd(self) -> None:
        """Migration v8 must convert existing net_backend='nm' rows to 'networkd'.

        Simulates production databases that have old incremental migrations v1-v7
        already applied (consolidated into single v1 in current code).
        """
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Apply only migration v1 (simulate pre-v8 database)
        v1_sql = MIGRATIONS[0][1]
        conn.executescript(v1_sql)
        # Simulate production: old migrations v1-v7 recorded
        for v in range(1, 8):
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (v,))
        conn.commit()

        # Insert peers with net_backend='nm' (old state)
        conn.execute("INSERT INTO nodes(node_id, created_at, updated_at) VALUES ('n1', '2024-01-01', '2024-01-01')")
        conn.execute(
            "INSERT INTO bgp_peers(node_id, peer_asn, ifname, wg_private_key, wg_public_key,"
            " peer_public_key, endpoint, local_lla, peer_lla, listen_port, allowed_ips_json, net_backend,"
            " created_at, updated_at)"
            " VALUES ('n1', 4242421111, 'dn42_1111', 'pk', 'pub', 'ppub', 'ep:1', 'fe80::1', 'fe80::2',"
            " 51820, '[\"fe80::/64\"]', 'nm', '2024-01-01', '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO ibgp_peers(node_id, name, ifname, wg_private_key, wg_public_key,"
            " peer_public_key, endpoint, local_lla, peer_lla, listen_port, allowed_ips_json, net_backend,"
            " babel_rxcost, peer_ip, has_wg, babel_type, created_at, updated_at)"
            " VALUES ('n1', 'test', 'wg_test', 'pk', 'pub', 'ppub', 'ep:2', 'fe80::3', 'fe80::4',"
            " 51821, '[\"fe80::/64\"]', 'nm', 20, 'fd00::1', 1, 'tunnel', '2024-01-01', '2024-01-01')"
        )
        conn.commit()

        db = Database(conn)
        db.migrate()

        bgp_row = conn.execute("SELECT net_backend FROM bgp_peers WHERE peer_asn=4242421111").fetchone()
        assert bgp_row["net_backend"] == "networkd", f"BGP peer not converted: {bgp_row['net_backend']}"

        ibgp_row = conn.execute("SELECT net_backend FROM ibgp_peers WHERE name='test'").fetchone()
        assert ibgp_row["net_backend"] == "networkd", f"iBGP peer not converted: {ibgp_row['net_backend']}"


class TestEnsureNode:
    def test_insert_and_upsert(self, mem_db: Database) -> None:
        mem_db.ensure_node("node-1")
        mem_db.ensure_node("node-1")
        rows = mem_db._conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id='node-1'").fetchone()
        assert rows[0] == 1


def _make_bgp_record(**overrides: object) -> BgpPeerRecord:
    defaults = {
        "node_id": "test-node",
        "ifname": "dn42_1234",
        "wg_private_key": "privkey",
        "wg_public_key": "pubkey",
        "peer_public_key": "peerpubkey",
        "endpoint": "example.com:51820",
        "local_lla": "fe80::abcd:1234",
        "peer_lla": "fe80::1",
        "listen_port": 51820,
        "allowed_ips": ["fe80::/64", "fd00::/8"],
        "net_backend": "networkd",
        "peer_asn": 4242421234,
    }
    defaults.update(overrides)
    return BgpPeerRecord(**defaults)  # type: ignore[arg-type]


def _make_ibgp_record(**overrides: object) -> IbgpPeerRecord:
    defaults = {
        "node_id": "test-node",
        "name": "mynode",
        "ifname": "wg_mynode",
        "wg_private_key": "privkey",
        "wg_public_key": "pubkey",
        "peer_public_key": "peerpubkey",
        "endpoint": "example.com:51821",
        "local_lla": "fe80::abcd:5678",
        "peer_lla": "fe80::2",
        "listen_port": 51821,
        "allowed_ips": ["::/0"],
        "net_backend": "networkd",
        "babel_rxcost": 120,
        "peer_ip": "fd42:4242:5678::1",
        "has_wg": True,
        "babel_type": "tunnel",
    }
    defaults.update(overrides)
    return IbgpPeerRecord(**defaults)  # type: ignore[arg-type]


class TestBgpPeerCrud:
    def test_insert_and_get(self, mem_db_with_node: Database) -> None:
        rec = _make_bgp_record()
        mem_db_with_node.insert_bgp_peer(rec)
        row = mem_db_with_node.get_bgp_peer("test-node", 4242421234)
        assert row is not None
        assert row["peer_asn"] == 4242421234
        assert row["ifname"] == "dn42_1234"
        assert row["net_backend"] == "networkd"

    def test_duplicate_raises(self, mem_db_with_node: Database) -> None:
        rec = _make_bgp_record()
        mem_db_with_node.insert_bgp_peer(rec)
        with pytest.raises(DatabaseError, match="already exists"):
            mem_db_with_node.insert_bgp_peer(rec)

    def test_list_ordered(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_bgp_peer(_make_bgp_record(peer_asn=9999, ifname="dn42_9999"))
        mem_db_with_node.insert_bgp_peer(_make_bgp_record(peer_asn=1111, ifname="dn42_1111"))
        peers = mem_db_with_node.list_bgp_peers("test-node")
        assert len(peers) == 2
        assert peers[0]["peer_asn"] == 1111
        assert peers[1]["peer_asn"] == 9999

    def test_update(self, mem_db_with_node: Database) -> None:
        rec = _make_bgp_record()
        mem_db_with_node.insert_bgp_peer(rec)
        mem_db_with_node.update_bgp_peer(
            node_id="test-node",
            peer_asn=4242421234,
            peer_public_key="newpubkey",
            endpoint="new.example.com:51820",
            peer_lla="fe80::99",
            listen_port=60000,
            allowed_ips=["fe80::/64"],
            net_backend="nm",
        )
        row = mem_db_with_node.get_bgp_peer("test-node", 4242421234)
        assert row is not None
        assert row["peer_public_key"] == "newpubkey"
        assert row["endpoint"] == "new.example.com:51820"
        assert row["listen_port"] == 60000
        assert row["net_backend"] == "nm"

    def test_update_nonexistent_raises(self, mem_db_with_node: Database) -> None:
        with pytest.raises(DatabaseError, match="not found"):
            mem_db_with_node.update_bgp_peer(
                node_id="test-node",
                peer_asn=99999,
                peer_public_key=None,
                endpoint=None,
                peer_lla=None,
                listen_port=51820,
                allowed_ips=[],
                net_backend="networkd",
            )

    def test_delete(self, mem_db_with_node: Database) -> None:
        rec = _make_bgp_record()
        mem_db_with_node.insert_bgp_peer(rec)
        deleted = mem_db_with_node.delete_bgp_peer("test-node", 4242421234)
        assert deleted is not None
        assert mem_db_with_node.get_bgp_peer("test-node", 4242421234) is None

    def test_delete_nonexistent(self, mem_db_with_node: Database) -> None:
        result = mem_db_with_node.delete_bgp_peer("test-node", 99999)
        assert result is None


class TestIbgpPeerCrud:
    def test_insert_and_get(self, mem_db_with_node: Database) -> None:
        rec = _make_ibgp_record()
        mem_db_with_node.insert_ibgp_peer(rec)
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None
        assert row["name"] == "mynode"
        assert row["babel_rxcost"] == 120
        assert row["babel_type"] == "tunnel"
        assert row["peer_ip"] == "fd42:4242:5678::1"
        assert row["has_wg"] == 1

    def test_duplicate_raises(self, mem_db_with_node: Database) -> None:
        rec = _make_ibgp_record()
        mem_db_with_node.insert_ibgp_peer(rec)
        with pytest.raises(DatabaseError, match="already exists"):
            mem_db_with_node.insert_ibgp_peer(rec)

    def test_list_ordered(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(name="zzz", ifname="wg_zzz"))
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(name="aaa", ifname="wg_aaa"))
        peers = mem_db_with_node.list_ibgp_peers("test-node")
        assert len(peers) == 2
        assert peers[0]["name"] == "aaa"
        assert peers[1]["name"] == "zzz"

    def test_update(self, mem_db_with_node: Database) -> None:
        rec = _make_ibgp_record()
        mem_db_with_node.insert_ibgp_peer(rec)
        mem_db_with_node.update_ibgp_peer(
            node_id="test-node",
            name="mynode",
            peer_public_key="newpubkey",
            endpoint="new.example.com:51821",
            peer_lla="fe80::99",
            listen_port=60001,
            allowed_ips=["::/0"],
            net_backend="nm",
            babel_rxcost=256,
            peer_ip="fd42:4242:9999::1",
            babel_type="wired",
        )
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None
        assert row["babel_rxcost"] == 256
        assert row["babel_type"] == "wired"
        assert row["peer_ip"] == "fd42:4242:9999::1"

    def test_remote_node_id_round_trips(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(remote_node_id="node-a"))
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None
        assert row["remote_node_id"] == "node-a"

    def test_remote_node_id_defaults_to_null(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record())
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None
        assert row["remote_node_id"] is None

    def test_update_leaves_remote_node_id_alone_by_default(self, mem_db_with_node: Database) -> None:
        """不传 remote_node_id 时整列不进 SET —— proposal 接受/上报导入不知道这个关联,
        不能让它们静默把链接抹掉。"""
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(remote_node_id="node-a"))
        mem_db_with_node.update_ibgp_peer(
            node_id="test-node",
            name="mynode",
            peer_public_key="newpubkey",
            endpoint="new.example.com:51821",
            peer_lla="fe80::99",
            listen_port=60001,
            allowed_ips=["::/0"],
            net_backend="networkd",
            babel_rxcost=256,
            peer_ip="fd42:4242:9999::1",
            babel_type="wired",
        )
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None
        assert row["remote_node_id"] == "node-a"

    def test_update_can_set_and_clear_remote_node_id(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record())
        common = {
            "node_id": "test-node",
            "name": "mynode",
            "peer_public_key": "pk",
            "endpoint": "e.example.com:51821",
            "peer_lla": "fe80::99",
            "listen_port": 60001,
            "allowed_ips": ["::/0"],
            "net_backend": "networkd",
            "babel_rxcost": 120,
            "peer_ip": "fd42:4242:9999::1",
            "babel_type": "tunnel",
        }
        mem_db_with_node.update_ibgp_peer(**common, remote_node_id="node-b")  # type: ignore[arg-type]
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None and row["remote_node_id"] == "node-b"

        mem_db_with_node.update_ibgp_peer(**common, remote_node_id=None)  # type: ignore[arg-type]
        row = mem_db_with_node.get_ibgp_peer("test-node", "mynode")
        assert row is not None and row["remote_node_id"] is None

    def test_list_by_remote_spans_node_partitions(self, mem_db_with_node: Database) -> None:
        """传播要改写的是**其他节点**的行,所以查询必须跨 node_id 分区。"""
        mem_db_with_node.ensure_node("node-b")
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(name="from-self", remote_node_id="node-a"))
        mem_db_with_node.insert_ibgp_peer(
            _make_ibgp_record(node_id="node-b", name="from-b", ifname="wg_fromb", remote_node_id="node-a")
        )
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(name="unlinked", ifname="wg_unlinked"))

        rows = mem_db_with_node.list_ibgp_peers_by_remote("node-a")
        assert [(r["node_id"], r["name"]) for r in rows] == [
            ("node-b", "from-b"),
            ("test-node", "from-self"),
        ]

    def test_update_nonexistent_raises(self, mem_db_with_node: Database) -> None:
        with pytest.raises(DatabaseError, match="not found"):
            mem_db_with_node.update_ibgp_peer(
                node_id="test-node",
                name="nonexistent",
                peer_public_key=None,
                endpoint=None,
                peer_lla=None,
                listen_port=51821,
                allowed_ips=[],
                net_backend="networkd",
                babel_rxcost=120,
                peer_ip=None,
                babel_type="tunnel",
            )

    def test_delete(self, mem_db_with_node: Database) -> None:
        rec = _make_ibgp_record()
        mem_db_with_node.insert_ibgp_peer(rec)
        deleted = mem_db_with_node.delete_ibgp_peer("test-node", "mynode")
        assert deleted is not None
        assert mem_db_with_node.get_ibgp_peer("test-node", "mynode") is None

    def test_delete_nonexistent(self, mem_db_with_node: Database) -> None:
        result = mem_db_with_node.delete_ibgp_peer("test-node", "nonexistent")
        assert result is None


class TestGetUsedListenPorts:
    def test_combined(self, mem_db_with_node: Database) -> None:
        mem_db_with_node.insert_bgp_peer(_make_bgp_record(listen_port=51820))
        mem_db_with_node.insert_ibgp_peer(_make_ibgp_record(listen_port=51821))
        ports = mem_db_with_node.get_used_listen_ports("test-node")
        assert ports == {51820, 51821}

    def test_empty(self, mem_db_with_node: Database) -> None:
        ports = mem_db_with_node.get_used_listen_ports("test-node")
        assert ports == set()


class TestDatabaseOpen:
    def test_open_creates_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.sqlite3"
        db = Database.open(db_path)
        assert db_path.exists()
        db.close()

    def test_open_creates_parent_dirs(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sub" / "dir" / "test.sqlite3"
        db = Database.open(db_path)
        assert db_path.exists()
        db.close()
