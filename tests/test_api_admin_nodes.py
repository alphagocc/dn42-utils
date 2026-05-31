from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
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
        # Now gone.
        resp2 = admin_client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H)
        assert resp2.status_code == 400

    def test_add_requires_admin(self, admin_client: TestClient) -> None:
        # No auth header -> 401
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


class TestNodeCannotReachAdmin:
    def test_node_token_blocked_from_admin_nodes(self, admin_client: TestClient) -> None:
        _add_node(admin_client, NODE_A, "alpha")
        token = _rotate_token(admin_client, NODE_A)
        resp = admin_client.get(
            "/api/admin/nodes",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
