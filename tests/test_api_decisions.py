from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from conftest import VALID_ENDPOINT, VALID_PEER_LLA, VALID_PUBKEY
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig

ADMIN_TOKEN = "admin-secret"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture(autouse=True)
def _mock_wg(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from conftest import FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY

    with (
        patch(
            "dn42ctl.services.core.generate_wg_keypair",
            return_value=(FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY),
        ),
        patch(
            "dn42ctl.services.bgp.generate_random_lla",
            return_value="fe80::abcd:1234",
        ),
        patch(
            "dn42ctl.services.ibgp.generate_random_lla",
            return_value="fe80::abcd:5678",
        ),
    ):
        yield


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    yield TestClient(app)


def _register(client: TestClient) -> str:
    r1 = client.post("/api/admin/nodes", json={"node_id": NODE_A, "name": "alpha"}, headers=ADMIN_H)
    assert r1.status_code == 201, r1.text
    r2 = client.post(f"/api/admin/nodes/{NODE_A}/token", headers=ADMIN_H)
    return r2.json()["token"]


def _bgp_add_payload(asn: int = 4242421234) -> dict:
    return {
        "source": "push",
        "kind": "peer_add",
        "payload": {
            "peer_kind": "bgp",
            "peer": {
                "peer_asn": asn,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
                "net_backend": "networkd",
            },
        },
    }


class TestAcceptRoute:
    def test_accept(self, client: TestClient) -> None:
        token = _register(client)
        sub = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json=_bgp_add_payload(),
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        resp = client.post(f"/api/admin/proposals/{sub['id']}/accept", headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "accepted"

    def test_accept_unknown(self, client: TestClient) -> None:
        resp = client.post("/api/admin/proposals/9999/accept", headers=ADMIN_H)
        assert resp.status_code == 400

    def test_accept_requires_admin(self, client: TestClient) -> None:
        token = _register(client)
        sub = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json=_bgp_add_payload(),
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        resp = client.post(
            f"/api/admin/proposals/{sub['id']}/accept",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestRejectRoute:
    def test_reject(self, client: TestClient) -> None:
        token = _register(client)
        sub = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json=_bgp_add_payload(),
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        resp = client.post(
            f"/api/admin/proposals/{sub['id']}/reject",
            json={"reason": "nope"},
            headers=ADMIN_H,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        assert resp.json()["message"] == "nope"


class TestImportReportRoute:
    def test_import(self, client: TestClient) -> None:
        token = _register(client)
        rep = client.post(
            f"/api/v1/nodes/{NODE_A}/reports",
            json={
                "kind": "scan_result",
                "payload": {
                    "bgp_peers": [
                        {
                            "peer_asn": 4242421234,
                            "peer_public_key": VALID_PUBKEY,
                            "endpoint": VALID_ENDPOINT,
                            "peer_lla": VALID_PEER_LLA,
                            "net_backend": "networkd",
                        }
                    ],
                    "ibgp_peers": [],
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        resp = client.post(f"/api/admin/reports/{rep['id']}/import", headers=ADMIN_H)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bgp_created"] == 1
        assert body["bgp_skipped"] == 0


class TestAutoAcceptViaApi:
    def test_peer_add_auto_accept_writes_during_submit(self, client: TestClient) -> None:
        token = _register(client)
        token_h = {"Authorization": f"Bearer {token}"}
        # Switch policy.
        client.patch(
            f"/api/admin/nodes/{NODE_A}/policy",
            json={"peer_add": "auto_accept"},
            headers=ADMIN_H,
        )
        sub = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json=_bgp_add_payload(),
            headers=token_h,
        )
        assert sub.status_code == 201
        body = sub.json()
        assert body["status"] == "accepted"
        # The peer must land under NODE_A's desired state (not the central self).
        desired = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h).json()
        assert any(p["peer_asn"] == 4242421234 for p in desired["bgp_peers"])
