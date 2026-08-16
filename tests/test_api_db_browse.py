from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import BgpPeerRecord, Database
from dn42ctl.db_managed import ManagedNodeStore, RevisionStore, hash_token

ADMIN_TOKEN = "admin-secret-token"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"

SECRET_KEY = "SECRET-WG-PRIVATE-KEY-MUST-NOT-LEAK"
SECRET_TOKEN = "secret-node-token"  # noqa: S105 — 测试用固定值


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def admin_client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    db = Database.open(db_path)
    try:
        db.ensure_node(sample_config.node_id)
        store = ManagedNodeStore(db.connection)
        store.add(NODE_A, "alpha")
        store.set_token_hash(NODE_A, hash_token(SECRET_TOKEN))
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=NODE_A,
                peer_asn=4242420001,
                ifname="dn42_0001",
                wg_private_key=SECRET_KEY,
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
        RevisionStore(db.connection).record(
            node_id=NODE_A,
            revision="2026-01-01T00:00:00+00:00-abcdef12",
            generated_at="2026-01-01T00:00:00+00:00",
            payload={"bgp_peers": [{"wg_private_key": SECRET_KEY}]},
        )
    finally:
        db.close()
    yield TestClient(app)


class TestAuth:
    def test_list_requires_token(self, admin_client: TestClient) -> None:
        assert admin_client.get("/api/admin/db/tables").status_code == 401

    def test_browse_requires_token(self, admin_client: TestClient) -> None:
        assert admin_client.get("/api/admin/db/tables/bgp_peers").status_code == 401

    def test_node_token_is_forbidden(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/api/admin/db/tables",
            headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
        )
        assert resp.status_code == 403


class TestListTables:
    def test_lists_tables_with_counts(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/db/tables", headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        by_name = {t["name"]: t for t in resp.json()}
        assert by_name["bgp_peers"]["rows"] == 1
        assert by_name["bgp_peers"]["redacted"] == ["wg_private_key"]
        assert "sqlite_master" not in by_name


class TestWhitelist:
    @pytest.mark.parametrize("table", ["sqlite_master", "nope", "BGP_PEERS"])
    def test_unknown_table_404(self, admin_client: TestClient, table: str) -> None:
        resp = admin_client.get(f"/api/admin/db/tables/{table}", headers=ADMIN_H)
        assert resp.status_code == 404, resp.text


class TestRedaction:
    def test_private_key_never_returned(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/db/tables/bgp_peers", headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        assert SECRET_KEY not in resp.text
        assert resp.json()["rows"][0]["wg_private_key"] == "***"

    def test_token_hash_never_returned(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/db/tables/managed_nodes", headers=ADMIN_H)
        row = next(r for r in resp.json()["rows"] if r["node_id"] == NODE_A)
        assert row["api_token_hash"] == "***"
        assert "$argon2" not in resp.text

    def test_revision_payload_never_returned(self, admin_client: TestClient) -> None:
        """config_revisions.payload_json 内含每一个 WireGuard 私钥。"""
        resp = admin_client.get("/api/admin/db/tables/config_revisions", headers=ADMIN_H)
        assert SECRET_KEY not in resp.text
        assert resp.json()["rows"][0]["payload_json"].startswith("<payload:")

    def test_no_secret_leaks_across_any_table(self, admin_client: TestClient) -> None:
        """兜底:把每张表都翻一遍,私钥与 token hash 一次都不能出现。"""
        tables = [t["name"] for t in admin_client.get("/api/admin/db/tables", headers=ADMIN_H).json()]
        for table in tables:
            resp = admin_client.get(f"/api/admin/db/tables/{table}?limit=500", headers=ADMIN_H)
            assert resp.status_code == 200, (table, resp.text)
            assert SECRET_KEY not in resp.text, table
            assert "$argon2" not in resp.text, table


class TestPagination:
    def test_limit_offset_and_total(self, admin_client: TestClient) -> None:
        body = admin_client.get("/api/admin/db/tables/bgp_peers?limit=1&offset=0", headers=ADMIN_H).json()
        assert body["total"] == 1
        assert body["limit"] == 1
        assert body["offset"] == 0
        assert "peer_asn" in body["columns"]

    @pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1"])
    def test_out_of_range_rejected(self, admin_client: TestClient, query: str) -> None:
        resp = admin_client.get(f"/api/admin/db/tables/bgp_peers?{query}", headers=ADMIN_H)
        assert resp.status_code == 422, resp.text


class TestNodeFilter:
    def test_filters_by_node(self, admin_client: TestClient) -> None:
        assert (
            admin_client.get(f"/api/admin/db/tables/bgp_peers?node_id={NODE_A}", headers=ADMIN_H).json()["total"] == 1
        )
        other = admin_client.get(
            "/api/admin/db/tables/bgp_peers?node_id=99999999-9999-4999-8999-999999999999",
            headers=ADMIN_H,
        ).json()
        assert other["total"] == 0

    def test_json_response_shape(self, admin_client: TestClient) -> None:
        body = admin_client.get("/api/admin/db/tables/nodes", headers=ADMIN_H).json()
        assert set(body) == {"table", "columns", "rows", "total", "limit", "offset", "redacted"}
        json.dumps(body)  # 必须可序列化
