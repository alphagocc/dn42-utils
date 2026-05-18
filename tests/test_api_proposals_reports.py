from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig

ADMIN_TOKEN = "admin-secret"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    yield TestClient(app)


def _register(client: TestClient, node_id: str, name: str = "x") -> str:
    r1 = client.post("/api/admin/nodes", json={"node_id": node_id, "name": name}, headers=ADMIN_H)
    assert r1.status_code == 201, r1.text
    r2 = client.post(f"/api/admin/nodes/{node_id}/token", headers=ADMIN_H)
    assert r2.status_code == 200, r2.text
    return r2.json()["token"]


class TestProposalsRoute:
    def test_node_posts_own_proposal(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        resp = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={"source": "push", "kind": "peer_add", "payload": {"asn": 1}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["kind"] == "peer_add"

    def test_node_cannot_post_other(self, client: TestClient) -> None:
        token_a = _register(client, NODE_A, "alpha")
        _register(client, NODE_B, "beta")
        resp = client.post(
            f"/api/v1/nodes/{NODE_B}/proposals",
            json={"source": "push", "kind": "peer_add", "payload": {}},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 403

    def test_no_token(self, client: TestClient) -> None:
        _register(client, NODE_A)
        resp = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={"source": "push", "kind": "peer_add", "payload": {}},
        )
        assert resp.status_code == 401

    def test_invalid_kind(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        resp = client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={"source": "push", "kind": "bogus", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_admin_lists(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={"source": "push", "kind": "peer_add", "payload": {"n": 1}},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={"source": "scan", "kind": "peer_add", "payload": {"n": 2}},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(f"/api/admin/nodes/{NODE_A}/proposals", headers=ADMIN_H)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert all(p["status"] == "pending" for p in items)

    def test_node_token_cannot_list_admin(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        resp = client.get(
            f"/api/admin/nodes/{NODE_A}/proposals",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestReportsRoute:
    def test_node_posts_own_report(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        resp = client.post(
            f"/api/v1/nodes/{NODE_A}/reports",
            json={"kind": "apply_result", "payload": {"ok": True}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["kind"] == "apply_result"
        assert body["imported_at"] is None

    def test_admin_lists_filtered(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        client.post(
            f"/api/v1/nodes/{NODE_A}/reports",
            json={"kind": "apply_result", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            f"/api/v1/nodes/{NODE_A}/reports",
            json={"kind": "scan_result", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(f"/api/admin/nodes/{NODE_A}/reports?kind=apply_result", headers=ADMIN_H)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["kind"] == "apply_result"

    def test_invalid_report_kind(self, client: TestClient) -> None:
        token = _register(client, NODE_A)
        resp = client.post(
            f"/api/v1/nodes/{NODE_A}/reports",
            json={"kind": "weird", "payload": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


class TestProposalIsolation:
    def test_node_a_only_sees_own(self, client: TestClient) -> None:
        token_a = _register(client, NODE_A, "alpha")
        token_b = _register(client, NODE_B, "beta")
        client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={"source": "push", "kind": "peer_add", "payload": {"n": 1}},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        client.post(
            f"/api/v1/nodes/{NODE_B}/proposals",
            json={"source": "push", "kind": "peer_add", "payload": {"n": 2}},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        a_list = client.get(f"/api/admin/nodes/{NODE_A}/proposals", headers=ADMIN_H).json()
        b_list = client.get(f"/api/admin/nodes/{NODE_B}/proposals", headers=ADMIN_H).json()
        assert len(a_list) == 1
        assert len(b_list) == 1
        assert a_list[0]["payload"]["n"] == 1
        assert b_list[0]["payload"]["n"] == 2

    def test_unauthoritative_changes_not_in_bgp_table(self, client: TestClient, sample_config: AppConfig) -> None:
        """Proposals MUST NOT touch business tables."""
        token = _register(client, NODE_A)
        client.post(
            f"/api/v1/nodes/{NODE_A}/proposals",
            json={
                "source": "push",
                "kind": "peer_add",
                "payload": {"peer_asn": 4242421234, "ifname": "dn42_1234"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get("/api/bgp/peers?live=false", headers=ADMIN_H)
        assert resp.status_code == 200
        assert resp.json() == []
