from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import Database

VALID_PUBKEY = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="
VALID_ENDPOINT = "example.com:51820"
VALID_PEER_LLA = "fe80::1"
ADMIN_TOKEN = "admin-secret-token"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def admin_client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    db = Database.open(db_path)
    db.ensure_node(sample_config.node_id)
    db.close()
    with patch("dn42ctl.services.bgp.generate_random_lla", return_value="fe80::abcd:1234"):
        yield TestClient(app)


class TestBgpPeerRoutes:
    """Regression: GET /api/admin/bgp/peers must be registered and return 200."""

    def test_list_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/bgp/peers", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_live_false(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/bgp/peers?live=false", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_requires_auth(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/bgp/peers")
        assert resp.status_code == 401

    def test_create_and_list(self, admin_client: TestClient) -> None:
        body = {
            "peer_asn": 4242420001,
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
        }
        resp = admin_client.post("/api/admin/bgp/peers", json=body, headers=ADMIN_H)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert "ifname" in created

        listed = admin_client.get("/api/admin/bgp/peers?live=false", headers=ADMIN_H).json()
        assert len(listed) == 1
        assert listed[0]["peer_asn"] == 4242420001

    def test_modify(self, admin_client: TestClient) -> None:
        body = {
            "peer_asn": 4242420002,
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
        }
        resp = admin_client.post("/api/admin/bgp/peers", json=body, headers=ADMIN_H)
        assert resp.status_code == 201

        new_lla = "fe80::99"
        modify_body = {
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": new_lla,
        }
        resp = admin_client.put("/api/admin/bgp/peers/4242420002", json=modify_body, headers=ADMIN_H)
        assert resp.status_code == 200, resp.text

        listed = admin_client.get("/api/admin/bgp/peers?live=false", headers=ADMIN_H).json()
        modified = [p for p in listed if p["peer_asn"] == 4242420002]
        assert len(modified) == 1
        assert modified[0]["peer_lla"] == new_lla

    def test_delete(self, admin_client: TestClient) -> None:
        body = {
            "peer_asn": 4242420003,
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
        }
        resp = admin_client.post("/api/admin/bgp/peers", json=body, headers=ADMIN_H)
        assert resp.status_code == 201

        resp = admin_client.delete("/api/admin/bgp/peers/4242420003", headers=ADMIN_H)
        assert resp.status_code == 200

        listed = admin_client.get("/api/admin/bgp/peers?live=false", headers=ADMIN_H).json()
        assert not any(p["peer_asn"] == 4242420003 for p in listed)

    def test_create_invalid_asn(self, admin_client: TestClient) -> None:
        body = {
            "peer_asn": -1,
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
        }
        resp = admin_client.post("/api/admin/bgp/peers", json=body, headers=ADMIN_H)
        assert resp.status_code == 422


class TestIbgpPeerRoutes:
    """Regression: GET /api/admin/ibgp/peers must be registered and return 200."""

    def test_list_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/ibgp/peers", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_live_false(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/ibgp/peers?live=false", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []


class TestWgTunnelRoutes:
    """Regression: GET /api/admin/wg/tunnels must be registered and return 200."""

    def test_list_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/admin/wg/tunnels", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []


class TestGenconfRoute:
    """Regression: POST /api/admin/genconf must be registered (not 404)."""

    def test_genconf_requires_auth(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/admin/genconf", json={})
        assert resp.status_code == 401
