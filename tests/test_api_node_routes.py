from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import BgpPeerRecord, Database

ADMIN_TOKEN = "admin-secret"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    yield TestClient(app)


def _register_node_with_token(client: TestClient, node_id: str, name: str = "x") -> str:
    r1 = client.post("/api/admin/nodes", json={"node_id": node_id, "name": name}, headers=ADMIN_H)
    assert r1.status_code == 201, r1.text
    r2 = client.post(f"/api/admin/nodes/{node_id}/token", headers=ADMIN_H)
    assert r2.status_code == 200, r2.text
    return r2.json()["token"]


def _seed_peer(db_path: Path, node_id: str) -> None:
    db = Database.open(db_path)
    try:
        db.ensure_node(node_id)
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=node_id,
                peer_asn=4242421234,
                ifname="dn42_1234",
                wg_private_key=FAKE_PRIV,
                wg_public_key=FAKE_PUB,
                peer_public_key=FAKE_PUB,
                endpoint="peer.example:51820",
                local_lla="fe80::1/64",
                peer_lla="fe80::2",
                listen_port=21234,
                allowed_ips=["fe80::/64", "fd00::/8"],
                net_backend="networkd",
            )
        )
    finally:
        db.close()


class TestDesiredEndpoint:
    def test_node_can_pull_own_desired(self, client: TestClient, db_path: Path) -> None:
        token = _register_node_with_token(client, NODE_A)
        _seed_peer(db_path, NODE_A)
        resp = client.get(
            f"/api/v1/nodes/{NODE_A}/desired",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["node_id"] == NODE_A
        assert len(body["bgp_peers"]) == 1
        assert body["bgp_peers"][0]["peer_asn"] == 4242421234
        assert body["bgp_peers"][0]["wg_private_key"] == FAKE_PRIV
        assert body["revision"]

    def test_admin_can_pull_any(self, client: TestClient, db_path: Path) -> None:
        _register_node_with_token(client, NODE_A)
        _seed_peer(db_path, NODE_A)
        resp = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=ADMIN_H)
        assert resp.status_code == 200

    def test_node_cannot_pull_other(self, client: TestClient) -> None:
        token_a = _register_node_with_token(client, NODE_A, "alpha")
        _register_node_with_token(client, NODE_B, "beta")
        resp = client.get(
            f"/api/v1/nodes/{NODE_B}/desired",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 403

    def test_no_token(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/nodes/{NODE_A}/desired")
        assert resp.status_code == 401

    def test_unknown_node(self, client: TestClient) -> None:
        # Admin token + non-existent node_id -> 400 (Dn42CtlError "不存在")
        resp = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=ADMIN_H)
        assert resp.status_code == 400

    def test_pull_updates_last_seen(self, client: TestClient, db_path: Path) -> None:
        token = _register_node_with_token(client, NODE_A)
        before = client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H).json()
        assert before["last_seen_at"] is None
        client.get(
            f"/api/v1/nodes/{NODE_A}/desired",
            headers={"Authorization": f"Bearer {token}"},
        )
        after = client.get(f"/api/admin/nodes/{NODE_A}", headers=ADMIN_H).json()
        assert after["last_seen_at"] is not None
