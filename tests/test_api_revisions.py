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


def _register(client: TestClient) -> str:
    client.post("/api/admin/nodes", json={"node_id": NODE_A, "name": "x"}, headers=ADMIN_H)
    r = client.post(f"/api/admin/nodes/{NODE_A}/token", headers=ADMIN_H)
    return r.json()["token"]


def _seed_peer(db_path: Path, asn: int) -> None:
    db = Database.open(db_path)
    try:
        db.ensure_node(NODE_A)
        db.insert_bgp_peer(
            BgpPeerRecord(
                node_id=NODE_A,
                peer_asn=asn,
                ifname=f"dn42_{asn % 10000:04d}",
                wg_private_key=FAKE_PRIV,
                wg_public_key=FAKE_PUB,
                peer_public_key=FAKE_PUB,
                endpoint="ep:51820",
                local_lla="fe80::1",
                peer_lla="fe80::2",
                listen_port=20000 + (asn % 10000),
                allowed_ips=["fe80::/64", "fd00::/8"],
                net_backend="networkd",
            )
        )
    finally:
        db.close()


class TestRevisionsRoute:
    def test_lists(self, client: TestClient, db_path: Path) -> None:
        token = _register(client)
        _seed_peer(db_path, 4242421111)
        client.get(f"/api/v1/nodes/{NODE_A}/desired", headers={"Authorization": f"Bearer {token}"})
        _seed_peer(db_path, 4242422222)
        client.get(f"/api/v1/nodes/{NODE_A}/desired", headers={"Authorization": f"Bearer {token}"})
        resp = client.get(f"/api/admin/nodes/{NODE_A}/revisions", headers=ADMIN_H)
        assert resp.status_code == 200
        body = resp.json()
        assert body["pinned_revision"] is None
        assert len(body["revisions"]) == 2


class TestRollbackRoute:
    def test_pin_and_pull(self, client: TestClient, db_path: Path) -> None:
        token = _register(client)
        token_h = {"Authorization": f"Bearer {token}"}
        _seed_peer(db_path, 4242421111)
        first = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h).json()
        first_rev = first["revision"]
        _seed_peer(db_path, 4242422222)
        client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h)

        resp = client.post(
            f"/api/admin/nodes/{NODE_A}/rollback",
            json={"revision": first_rev},
            headers=ADMIN_H,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["pinned"]["revision"] == first_rev

        # Pull again: must return first revision (1 peer), not latest (2 peers).
        pulled = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h).json()
        assert pulled["revision"] == first_rev
        assert len(pulled["bgp_peers"]) == 1

    def test_rollback_unknown_revision(self, client: TestClient) -> None:
        _register(client)
        resp = client.post(
            f"/api/admin/nodes/{NODE_A}/rollback",
            json={"revision": "no-such"},
            headers=ADMIN_H,
        )
        assert resp.status_code == 400

    def test_clear(self, client: TestClient, db_path: Path) -> None:
        token = _register(client)
        token_h = {"Authorization": f"Bearer {token}"}
        _seed_peer(db_path, 4242421111)
        first = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h).json()
        _seed_peer(db_path, 4242422222)
        client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h)
        client.post(
            f"/api/admin/nodes/{NODE_A}/rollback",
            json={"revision": first["revision"]},
            headers=ADMIN_H,
        )
        resp = client.delete(f"/api/admin/nodes/{NODE_A}/rollback", headers=ADMIN_H)
        assert resp.status_code == 200
        # Pull now returns latest again.
        pulled = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers=token_h).json()
        assert len(pulled["bgp_peers"]) == 2

    def test_node_token_cannot_rollback(self, client: TestClient, db_path: Path) -> None:
        token = _register(client)
        _seed_peer(db_path, 4242421111)
        first = client.get(f"/api/v1/nodes/{NODE_A}/desired", headers={"Authorization": f"Bearer {token}"}).json()
        resp = client.post(
            f"/api/admin/nodes/{NODE_A}/rollback",
            json={"revision": first["revision"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
