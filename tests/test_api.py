from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import VALID_ENDPOINT, VALID_PEER_IP, VALID_PEER_LLA, VALID_PUBKEY
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import Database

HEADERS = {"Authorization": "Bearer test-secret"}
BAD_HEADERS = {"Authorization": "Bearer wrong-token"}


@pytest.fixture
def api_client(sample_config: AppConfig, db_path: Path, mock_wg_keypair):
    configure(config=sample_config, db_path=db_path, token="test-secret")
    db = Database.open(db_path)
    db.ensure_node(sample_config.node_id)
    db.close()
    with (
        patch("dn42ctl.services.bgp.generate_random_lla", return_value="fe80::abcd:1234"),
        patch("dn42ctl.services.ibgp.generate_random_lla", return_value="fe80::abcd:5678"),
    ):
        client = TestClient(app)
        yield client


class TestAuth:
    def test_no_token(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/bgp/peers")
        assert resp.status_code in (401, 403)

    def test_wrong_token(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/bgp/peers", headers=BAD_HEADERS)
        assert resp.status_code == 401

    def test_valid_token(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/bgp/peers?live=false", headers=HEADERS)
        assert resp.status_code == 200


class TestBgpPeerApi:
    def test_create(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/bgp/peers",
            json={
                "peer_asn": 4242421234,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
                "net_backend": "networkd",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["ifname"] == "dn42_1234"
        assert "listen_port" in data
        assert "wg_public_key" in data

    def test_create_duplicate(self, api_client: TestClient) -> None:
        body = {
            "peer_asn": 4242421234,
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
        }
        api_client.post("/api/bgp/peers", json=body, headers=HEADERS)
        resp = api_client.post("/api/bgp/peers", json=body, headers=HEADERS)
        assert resp.status_code == 400

    def test_list(self, api_client: TestClient) -> None:
        api_client.post(
            "/api/bgp/peers",
            json={
                "peer_asn": 4242421234,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
            },
            headers=HEADERS,
        )
        resp = api_client.get("/api/bgp/peers?live=false", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["peer_asn"] == 4242421234

    def test_modify(self, api_client: TestClient) -> None:
        api_client.post(
            "/api/bgp/peers",
            json={
                "peer_asn": 4242421234,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
            },
            headers=HEADERS,
        )
        resp = api_client.put(
            "/api/bgp/peers/4242421234",
            json={
                "peer_public_key": VALID_PUBKEY,
                "endpoint": "new.example.com:51820",
                "peer_lla": "fe80::99",
                "net_backend": "networkd",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200

    def test_delete(self, api_client: TestClient) -> None:
        api_client.post(
            "/api/bgp/peers",
            json={
                "peer_asn": 4242421234,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
            },
            headers=HEADERS,
        )
        resp = api_client.delete("/api/bgp/peers/4242421234", headers=HEADERS)
        assert resp.status_code == 200

    def test_validation_error(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/bgp/peers",
            json={
                "peer_asn": -1,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
            },
            headers=HEADERS,
        )
        assert resp.status_code == 422


class TestIbgpPeerApi:
    def test_create(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/ibgp/peers",
            json={
                "name": "mynode",
                "peer_ip": VALID_PEER_IP,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
                "net_backend": "networkd",
                "babel_rxcost": 120,
                "babel_type": "tunnel",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["ifname"] == "wg_mynode"

    def test_create_no_wg(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/ibgp/peers",
            json={
                "name": "no_wg",
                "peer_ip": VALID_PEER_IP,
                "has_wg": False,
            },
            headers=HEADERS,
        )
        assert resp.status_code == 201

    def test_list(self, api_client: TestClient) -> None:
        api_client.post(
            "/api/ibgp/peers",
            json={
                "name": "mynode",
                "peer_ip": VALID_PEER_IP,
                "has_wg": False,
            },
            headers=HEADERS,
        )
        resp = api_client.get("/api/ibgp/peers?live=false", headers=HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_delete(self, api_client: TestClient) -> None:
        api_client.post(
            "/api/ibgp/peers",
            json={
                "name": "mynode",
                "peer_ip": VALID_PEER_IP,
                "has_wg": False,
            },
            headers=HEADERS,
        )
        resp = api_client.delete("/api/ibgp/peers/mynode", headers=HEADERS)
        assert resp.status_code == 200


class TestShowAll:
    def test_show_all(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/show/all?live=false", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "node_id" in data
        assert "wg" in data
        assert "bgp" in data
        assert "ibgp" in data


class TestWgTunnels:
    def test_show_wg(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/wg/tunnels?live=false", headers=HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
