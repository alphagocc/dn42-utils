from __future__ import annotations

import json
from pathlib import Path

import pytest

from dn42ctl.db import BgpPeerRecord, Database, IbgpPeerRecord
from dn42ctl.db_managed import ManagedNodeStore, RevisionStore, hash_token
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.db_browse import REDACTED, browse_table, list_tables

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"

SECRET_BGP_KEY = "SECRET-BGP-PRIVATE-KEY-DO-NOT-LEAK"
SECRET_IBGP_KEY = "SECRET-IBGP-PRIVATE-KEY-DO-NOT-LEAK"
SECRET_TOKEN = "super-secret-node-token"  # noqa: S105 — 测试用固定值


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "dn42.sqlite3"
    db = Database.open(path)
    try:
        store = ManagedNodeStore(db.connection)
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        store.set_token_hash(NODE_A, hash_token(SECRET_TOKEN))

        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=NODE_A,
                peer_asn=4242420001,
                ifname="dn42_0001",
                wg_private_key=SECRET_BGP_KEY,
                wg_public_key="pub",
                peer_public_key="peerpub",
                endpoint="a.example.com:51820",
                local_lla="fe80::1",
                peer_lla="fe80::2",
                listen_port=31000,
                allowed_ips=["::/0"],
                net_backend="networkd",
            )
        )
        db.insert_ibgp_peer(
            IbgpPeerRecord(
                node_id=NODE_A,
                name="site-b",
                ifname="wg_site_b",
                wg_private_key=SECRET_IBGP_KEY,
                wg_public_key="pub",
                peer_public_key="peerpub",
                endpoint="b.example.com:51821",
                local_lla="fe80::1",
                peer_lla="fe80::2",
                listen_port=31001,
                allowed_ips=["::/0"],
                net_backend="networkd",
                babel_rxcost=20,
                peer_ip="fd42:4242:2222::1",
            )
        )
        # config_revisions 存的是完整 desired-state 快照,内含每一个私钥。
        RevisionStore(db.connection).record(
            node_id=NODE_A,
            revision="2026-01-01T00:00:00+00:00-abcdef12",
            generated_at="2026-01-01T00:00:00+00:00",
            payload={"bgp_peers": [{"wg_private_key": SECRET_BGP_KEY}]},
        )
    finally:
        db.close()
    return path


class TestListTables:
    def test_lists_all_browsable_tables(self, db_path: Path) -> None:
        names = {t.name for t in list_tables(db_path=db_path)}
        assert names == {
            "schema_migrations",
            "nodes",
            "bgp_peers",
            "ibgp_peers",
            "managed_nodes",
            "config_proposals",
            "node_reports",
            "config_revisions",
            "node_desired_pin",
            "sync_events",
        }

    def test_reports_row_counts_and_redacted_columns(self, db_path: Path) -> None:
        by_name = {t.name: t for t in list_tables(db_path=db_path)}
        assert by_name["bgp_peers"].rows == 1
        assert by_name["bgp_peers"].redacted == ["wg_private_key"]
        assert by_name["managed_nodes"].rows == 2
        assert by_name["managed_nodes"].redacted == ["api_token_hash"]
        assert by_name["nodes"].redacted == []


class TestWhitelist:
    @pytest.mark.parametrize(
        "table",
        ["sqlite_master", "does_not_exist", "bgp_peers; DROP TABLE nodes", "BGP_PEERS", ""],
    )
    def test_non_whitelisted_table_rejected(self, db_path: Path, table: str) -> None:
        with pytest.raises(Dn42CtlError, match="不可浏览的表"):
            browse_table(db_path=db_path, table=table)

    def test_sqlite_master_still_intact_after_injection_attempt(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError):
            browse_table(db_path=db_path, table="bgp_peers; DROP TABLE nodes")
        db = Database.open(db_path)
        try:
            tables = {r[0] for r in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        assert "nodes" in tables


class TestRedaction:
    """漏一处就等于把全网 WireGuard 私钥挂在 web 上。"""

    def test_bgp_private_key_redacted(self, db_path: Path) -> None:
        page = browse_table(db_path=db_path, table="bgp_peers")
        assert page.rows[0]["wg_private_key"] == REDACTED
        assert SECRET_BGP_KEY not in json.dumps(page.rows)

    def test_ibgp_private_key_redacted(self, db_path: Path) -> None:
        page = browse_table(db_path=db_path, table="ibgp_peers")
        assert page.rows[0]["wg_private_key"] == REDACTED
        assert SECRET_IBGP_KEY not in json.dumps(page.rows)

    def test_api_token_hash_redacted(self, db_path: Path) -> None:
        page = browse_table(db_path=db_path, table="managed_nodes")
        by_id = {r["node_id"]: r for r in page.rows}
        assert by_id[NODE_A]["api_token_hash"] == REDACTED
        # 没签发 token 的节点保留 NULL —— 这正是 Nodes 页在用的 has_token 语义。
        assert by_id[NODE_B]["api_token_hash"] is None

    def test_revision_payload_redacted(self, db_path: Path) -> None:
        """config_revisions.payload_json 是最容易漏的一处:它内含每一个私钥,
        而现有的 _revision_to_dict 刻意从不返回它。"""
        page = browse_table(db_path=db_path, table="config_revisions")
        payload = page.rows[0]["payload_json"]
        assert payload.startswith("<payload:")
        assert SECRET_BGP_KEY not in json.dumps(page.rows)

    def test_no_prefix_leaked(self, db_path: Path) -> None:
        """绝不给前缀:token 摘要前缀让离线比对可行,WG 私钥前缀缩小密钥空间。"""
        page = browse_table(db_path=db_path, table="bgp_peers")
        value = page.rows[0]["wg_private_key"]
        assert value == REDACTED
        assert not any(value.startswith(SECRET_BGP_KEY[:n]) for n in range(1, 6))

    def test_non_secret_columns_pass_through(self, db_path: Path) -> None:
        page = browse_table(db_path=db_path, table="bgp_peers")
        assert page.rows[0]["peer_asn"] == 4242420001
        assert page.rows[0]["wg_public_key"] == "pub"

    def test_proposal_and_report_payloads_not_redacted(self, db_path: Path) -> None:
        """这两个 payload 已被现有 admin 路由全量返回,这里再遮一层只是自欺。"""
        db = Database.open(db_path)
        try:
            db.connection.execute(
                "INSERT INTO config_proposals(node_id,source,kind,payload_json,received_at) VALUES (?,?,?,?,?)",
                (NODE_A, "push", "peer_add", '{"visible": true}', "2026-01-01T00:00:00+00:00"),
            )
            db.connection.commit()
        finally:
            db.close()
        page = browse_table(db_path=db_path, table="config_proposals")
        assert page.rows[0]["payload_json"] == '{"visible": true}'
        assert page.redacted == []


class TestPagination:
    def test_limit_and_offset(self, db_path: Path) -> None:
        page = browse_table(db_path=db_path, table="managed_nodes", limit=1, offset=0)
        assert page.total == 2
        assert len(page.rows) == 1
        assert page.limit == 1
        assert page.offset == 0

        page2 = browse_table(db_path=db_path, table="managed_nodes", limit=1, offset=1)
        assert page2.rows[0]["node_id"] != page.rows[0]["node_id"]

    def test_limit_is_clamped(self, db_path: Path) -> None:
        assert browse_table(db_path=db_path, table="nodes", limit=99999).limit == 500
        assert browse_table(db_path=db_path, table="nodes", limit=0).limit == 1
        assert browse_table(db_path=db_path, table="nodes", offset=-5).offset == 0

    def test_columns_reported(self, db_path: Path) -> None:
        page = browse_table(db_path=db_path, table="bgp_peers")
        assert "peer_asn" in page.columns
        assert "wg_private_key" in page.columns


class TestNodeFilter:
    def test_filters_node_scoped_table(self, db_path: Path) -> None:
        assert browse_table(db_path=db_path, table="bgp_peers", node_id=NODE_A).total == 1
        assert browse_table(db_path=db_path, table="bgp_peers", node_id=NODE_B).total == 0

    def test_ignored_for_non_scoped_table(self, db_path: Path) -> None:
        """schema_migrations 没有 node_id 列,传了也不能让 SQL 炸掉。"""
        page = browse_table(db_path=db_path, table="schema_migrations", node_id=NODE_A)
        assert page.total > 0
