from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from conftest import VALID_PUBKEY
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import Database

ADMIN_TOKEN = "admin-secret-token"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def admin_client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    db = Database.open(db_path)
    db.ensure_node(sample_config.node_id)
    db.close()
    with patch("dn42ctl.services.bgp.generate_random_lla", return_value="fe80::abcd:1234"):
        yield TestClient(app)


def _add_node(client: TestClient, node_id: str, name: str) -> dict:
    resp = client.post("/api/admin/nodes", json={"node_id": node_id, "name": name}, headers=ADMIN_H)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rotate_token(client: TestClient, node_id: str) -> str:
    resp = client.post(f"/api/admin/nodes/{node_id}/token", headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestPrincipalResolution:
    def test_admin_route_admin_ok(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/nodes", headers=ADMIN_H)
        assert resp.status_code == 200

    def test_admin_route_no_token(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/nodes")
        assert resp.status_code == 401

    def test_admin_route_unknown_token(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/api/admin/nodes",
            headers={"Authorization": "Bearer nope"},
        )
        assert resp.status_code == 401

    def test_admin_route_node_token_forbidden(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        token = _rotate_token(admin_client, NODE_A)
        resp = admin_client.get(
            "/api/admin/nodes",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestAdminNodesCrud:
    def test_add_list(self, admin_client: TestClient) -> None:
        data = _add_node(admin_client, NODE_A, "alpha")
        assert data["node_id"] == NODE_A
        assert data["name"] == "alpha"
        assert data["is_self"] is False
        assert data["has_token"] is False
        listed = admin_client.get("/api/admin/nodes", headers=ADMIN_H).json()
        assert any(n["node_id"] == NODE_A for n in listed)

    def test_add_invalid_uuid(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/admin/nodes", json={"node_id": "bad", "name": "x"}, headers=ADMIN_H)
        assert resp.status_code == 400

    def test_get(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json()["name"] == "alpha"

    def test_get_missing(self, admin_client: TestClient) -> None:
        resp = admin_client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H)
        assert resp.status_code == 400

    def test_remove(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.delete(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H)
        assert resp.status_code == 200
        resp2 = admin_client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H)
        assert resp2.status_code == 400

    def test_add_requires_admin(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/admin/nodes", json={"node_id": NODE_A, "name": "alpha"})
        assert resp.status_code == 401


class TestRotateToken:
    def test_token_round_trip(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        token = _rotate_token(admin_client, NODE_A)
        node = admin_client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H).json()
        assert node["has_token"] is True
        resp = admin_client.get(
            "/api/admin/nodes",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestSetPolicy:
    def test_partial(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}/policy",
            json={"peer_add": "auto_accept"},
            headers=ADMIN_H,
        )
        assert resp.status_code == 200
        assert resp.json()["write_policy"]["peer_add"] == "auto_accept"
        assert resp.json()["write_policy"]["peer_modify"] == "review"

    def test_invalid_peer_modify(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}/policy",
            json={"peer_modify": "auto_accept"},
            headers=ADMIN_H,
        )
        assert resp.status_code == 400


class TestPatchNode:
    def test_rename(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"name": "renamed"}, headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "renamed"

    def test_set_addresses(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}",
            json={"endpoint_host": "a.example.com", "own_ipv6": "fd42:4242:1::1", "router_id": "172.20.1.1"},
            headers=ADMIN_H,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["endpoint_host"] == "a.example.com"
        assert body["own_ipv6"] == "fd42:4242:1::1"
        assert body["router_id"] == "172.20.1.1"

    def test_absent_field_is_untouched(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"own_ipv6": "fd42:4242:1::1"}, headers=ADMIN_H)
        resp = admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"name": "renamed"}, headers=ADMIN_H)
        assert resp.json()["own_ipv6"] == "fd42:4242:1::1"

    def test_explicit_null_clears(self, admin_client: TestClient) -> None:
        """字段缺席 = 不改动;显式 null = 取消中心管理。两者必须区分开。"""
        _add_node(admin_client, NODE_A, "alpha")
        admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"own_ipv6": "fd42:4242:1::1"}, headers=ADMIN_H)
        resp = admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"own_ipv6": None}, headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        assert resp.json()["own_ipv6"] is None

    def test_disable_and_enable(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        assert (
            admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"enabled": False}, headers=ADMIN_H).json()["enabled"]
            is False
        )
        assert (
            admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"enabled": True}, headers=ADMIN_H).json()["enabled"]
            is True
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"own_ipv6": "not-an-ipv6"},
            {"router_id": "999.999.999.999"},
            {"endpoint_host": "a.example.com:51820"},
            {"endpoint_host": "[2001:db8::1]"},
        ],
    )
    def test_invalid_values_rejected(self, admin_client: TestClient, payload: dict) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(f"/api/admin/nodes/{NODE_A}", json=payload, headers=ADMIN_H)
        assert resp.status_code == 422, resp.text

    def test_unknown_node_400(self, admin_client: TestClient) -> None:
        resp = admin_client.patch(f"/api/admin/nodes/{NODE_B}", json={"name": "x"}, headers=ADMIN_H)
        assert resp.status_code == 400

    def test_requires_admin(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        assert admin_client.patch(f"/api/admin/nodes/{NODE_A}", json={"name": "x"}).status_code == 401

    def test_dry_run_reports_without_writing(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}",
            json={"own_ipv6": "fd42:4242:1::1", "dry_run": True},
            headers=ADMIN_H,
        )
        assert resp.json()["dry_run"] is True
        after = admin_client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H).json()
        assert after["own_ipv6"] is None

    def test_propagation_reported(self, admin_client: TestClient, sample_config: AppConfig) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.post(
            "/api/admin/ibgp/peers",
            json={
                "name": "to-a",
                "peer_ip": "fd42:4242:1::1",
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "old.example.com:51821",
                "peer_lla": "fe80::2",
                "remote_node_id": NODE_A,
            },
            headers=ADMIN_H,
        )
        assert resp.status_code == 201, resp.text

        patched = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}",
            json={"endpoint_host": "new.example.com", "own_ipv6": "fd42:4242:1::9"},
            headers=ADMIN_H,
        )
        assert patched.status_code == 200, patched.text
        propagated = {(c["field"], c["new"]) for c in patched.json()["propagated"]}
        assert propagated == {
            ("peer_ip", "fd42:4242:1::9"),
            ("endpoint", "new.example.com:51821"),
        }

        peers = admin_client.get("/api/admin/ibgp/peers?live=false", headers=ADMIN_H).json()
        assert peers[0]["endpoint"] == "new.example.com:51821"
        assert peers[0]["peer_ip"] == "fd42:4242:1::9"

    def test_warnings_surfaced_when_nothing_linked(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}",
            json={"own_ipv6": "fd42:4242:1::9"},
            headers=ADMIN_H,
        )
        assert any("没有任何 iBGP peer 行" in w for w in resp.json()["warnings"])

    def test_no_propagate(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        admin_client.post(
            "/api/admin/ibgp/peers",
            json={
                "name": "to-a",
                "peer_ip": "fd42:4242:1::1",
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "old.example.com:51821",
                "peer_lla": "fe80::2",
                "remote_node_id": NODE_A,
            },
            headers=ADMIN_H,
        )
        resp = admin_client.patch(
            f"/api/admin/nodes/{NODE_A}",
            json={"own_ipv6": "fd42:4242:1::9", "propagate": False},
            headers=ADMIN_H,
        )
        assert resp.json()["propagated"] == []
        peers = admin_client.get("/api/admin/ibgp/peers?live=false", headers=ADMIN_H).json()
        assert peers[0]["peer_ip"] == "fd42:4242:1::1"


class TestNodeScopedPeerRoutes:
    def test_peer_created_under_explicit_node_is_partitioned(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        resp = admin_client.post(
            f"/api/admin/bgp/peers?node_id={NODE_A}",
            json={
                "peer_asn": 4242425678,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "b.example.com:51820",
                "peer_lla": "fe80::2",
            },
            headers=ADMIN_H,
        )
        assert resp.status_code == 201, resp.text

        assert admin_client.get("/api/admin/bgp/peers?live=false", headers=ADMIN_H).json() == []
        scoped = admin_client.get(f"/api/admin/bgp/peers?live=false&node_id={NODE_A}", headers=ADMIN_H).json()
        assert [p["peer_asn"] for p in scoped] == [4242425678]

    def test_remote_node_files_are_empty(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        admin_client.post(
            f"/api/admin/bgp/peers?node_id={NODE_A}",
            json={
                "peer_asn": 4242425678,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "b.example.com:51820",
                "peer_lla": "fe80::2",
            },
            headers=ADMIN_H,
        )
        scoped = admin_client.get(f"/api/admin/bgp/peers?live=false&node_id={NODE_A}", headers=ADMIN_H).json()
        assert scoped[0]["files"] == []

    def test_delete_honours_node_scope(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        admin_client.post(
            f"/api/admin/bgp/peers?node_id={NODE_A}",
            json={
                "peer_asn": 4242425678,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "b.example.com:51820",
                "peer_lla": "fe80::2",
            },
            headers=ADMIN_H,
        )
        # 不带 node_id 时目标是 hub 自身,那里没有这条 peer
        assert admin_client.delete("/api/admin/bgp/peers/4242425678", headers=ADMIN_H).status_code == 400
        assert (
            admin_client.delete(f"/api/admin/bgp/peers/4242425678?node_id={NODE_A}", headers=ADMIN_H).status_code == 200
        )

    def test_show_all_reports_scope_metadata(self, admin_client: TestClient, sample_config: AppConfig) -> None:
        body = admin_client.get("/api/show/all?live=false", headers=ADMIN_H).json()
        assert body["config_node_id"] == sample_config.node_id
        assert body["node_id_mismatch"] is False

    def test_default_scope_follows_self_node(self, admin_client: TestClient, db_path: Path) -> None:
        """默认作用域必须跟 desired-state 用的是同一个 node_id,否则写进去的 peer 永远不下发。"""
        from dn42ctl.db_managed import ManagedNodeStore

        db = Database.open(db_path)
        try:
            ManagedNodeStore(db.connection).upsert_self(NODE_B, name="self")
        finally:
            db.close()

        resp = admin_client.post(
            "/api/admin/bgp/peers",
            json={
                "peer_asn": 4242429999,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "c.example.com:51820",
                "peer_lla": "fe80::2",
            },
            headers=ADMIN_H,
        )
        assert resp.status_code == 201, resp.text

        scoped = admin_client.get(f"/api/admin/bgp/peers?live=false&node_id={NODE_B}", headers=ADMIN_H).json()
        assert [p["peer_asn"] for p in scoped] == [4242429999]
        assert admin_client.get("/api/show/all?live=false", headers=ADMIN_H).json()["node_id"] == NODE_B
