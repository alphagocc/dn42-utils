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


class TestEmptyPeerLists:
    """Regression: the admin BGP, iBGP and WG list routes must be registered."""

    @pytest.mark.parametrize(
        "route", ["/api/admin/bgp/peers", "/api/admin/ibgp/peers", "/api/admin/wg/tunnels"], ids=["bgp", "ibgp", "wg"]
    )
    def test_list_empty(self, admin_client: TestClient, route: str) -> None:
        resp = admin_client.get(route, headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []


class TestBgpPeerRoutes:
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


class TestGenconfRoute:
    """Regression: POST /api/admin/genconf must be registered (not 404)."""

    def test_genconf_requires_auth(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/admin/genconf", json={})
        assert resp.status_code == 401

    def test_overwrite_flags_control_generation(self, admin_client: TestClient, sample_config: AppConfig) -> None:
        bird = Path(sample_config.bird_conf_path)
        bird.write_text("operator config\n")
        Path(sample_config.bird_babel_conf_path).write_text("# babel\n")
        Path(sample_config.bird_roa_v6_conf_path).write_text("# ROA\n")
        with patch("dn42ctl.services.dummy.subprocess.check_output", return_value=""):
            refused = admin_client.post("/api/admin/genconf", json={}, headers=ADMIN_H)
            assert refused.status_code == 400
            assert bird.read_text() == "operator config\n"
            response = admin_client.post(
                "/api/admin/genconf",
                json={"overwrite_bird_conf": True, "overwrite_babel_conf": True},
                headers=ADMIN_H,
            )
        assert response.status_code == 200, response.text
        assert response.json()["bird_conf_path"] == str(bird)
        extra = Path(sample_config.bird_extra_conf_path)
        assert extra.exists()
        assert f'include "{extra}";' in bird.read_text()


@pytest.fixture
def ibgp_body() -> dict[str, object]:
    return {
        "name": "leaf",
        "has_wg": True,
        "peer_ip": "fd42:4242:1::1",
        "peer_public_key": VALID_PUBKEY,
        "endpoint": VALID_ENDPOINT,
        "peer_lla": VALID_PEER_LLA,
        "babel_rxcost": 120,
        "babel_type": "tunnel",
        "listen_port": 31002,
        "allowed_ips": ["fe80::1/64", "fd42::/16"],
    }


class TestIbgpPeerRoutes:
    @pytest.mark.parametrize(
        ("link_patch", "expected_link"),
        [
            ({}, "11111111-1111-4111-8111-111111111111"),
            ({"remote_node_id": None}, None),
            ({"remote_node_id": "22222222-2222-4222-8222-222222222222"}, "22222222-2222-4222-8222-222222222222"),
        ],
        ids=["omitted-link", "clear-link", "replace-link"],
    )
    def test_modify_persists_fields_and_honors_link_presence(
        self,
        admin_client: TestClient,
        sample_config: AppConfig,
        mock_wg_keypair,
        ibgp_body: dict[str, object],
        link_patch: dict[str, object],
        expected_link: str | None,
    ) -> None:
        created = admin_client.post(
            "/api/admin/ibgp/peers",
            json={**ibgp_body, "remote_node_id": "11111111-1111-4111-8111-111111111111"},
            headers=ADMIN_H,
        )
        assert created.status_code == 201, created.text
        assert created.json()["generated_files"] == []
        updated_body = {key: value for key, value in ibgp_body.items() if key not in {"name", "has_wg"}}
        updated_body.update(
            {
                "endpoint": "new.example:51900",
                "peer_lla": "fe80::99",
                "peer_ip": "2001:db8:2::99",
                "listen_port": 31003,
                "babel_rxcost": 256,
                "babel_type": "wired",
                "allowed_ips": ["2001:db8:2::1/48"],
                **link_patch,
            }
        )
        modified = admin_client.put("/api/admin/ibgp/peers/leaf", json=updated_body, headers=ADMIN_H)
        assert modified.status_code == 200, modified.text
        peer = admin_client.get("/api/admin/ibgp/peers", headers=ADMIN_H).json()[0]
        assert peer["endpoint"] == "new.example:51900"
        assert peer["peer_lla"] == "fe80::99"
        assert peer["peer_ip"] == "2001:db8:2::99"
        assert peer["listen_port"] == 31003
        assert peer["babel_rxcost"] == 256
        assert peer["babel_type"] == "wired"
        assert peer["allowed_ips"] == ["2001:db8:2::/48"]
        assert peer["remote_node_id"] == expected_link
        deleted = admin_client.delete("/api/admin/ibgp/peers/leaf", headers=ADMIN_H)
        assert deleted.status_code == 200, deleted.text
        assert admin_client.get("/api/admin/ibgp/peers", headers=ADMIN_H).json() == []
        assert list(Path(sample_config.bird_peers_dir).iterdir()) == []
        assert list(Path(sample_config.networkd_dir).iterdir()) == []

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("listen_port", -1),
            ("allowed_ips", ["10.0.0.0/8"]),
            ("babel_rxcost", -1),
            ("babel_type", "invalid"),
            ("peer_lla", "not-an-ip"),
        ],
        ids=["port", "allowed-ips", "rxcost", "babel-type", "peer-lla"],
    )
    def test_invalid_fields_are_rejected_before_creation(
        self, admin_client: TestClient, ibgp_body: dict[str, object], field: str, value: object
    ) -> None:
        response = admin_client.post("/api/admin/ibgp/peers", json={**ibgp_body, field: value}, headers=ADMIN_H)
        assert response.status_code == 422, response.text
        assert any(error["loc"] == ["body", field] for error in response.json()["detail"])
        assert admin_client.get("/api/admin/ibgp/peers", headers=ADMIN_H).json() == []

    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    def test_missing_peer_returns_client_error(
        self, admin_client: TestClient, ibgp_body: dict[str, object], method: str
    ) -> None:
        response = admin_client.request(
            method, "/api/admin/ibgp/peers/missing", json=ibgp_body if method == "PUT" else None, headers=ADMIN_H
        )
        assert response.status_code == 400
        assert "不存在" in response.json()["detail"]
