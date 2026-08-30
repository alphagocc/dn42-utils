"""Tests for the /api/public/auto-peer/* HTTP routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dn42ctl.api import app, configure
from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services.auto_peer import reset_state
from dn42ctl.services.kioubit_auth import KioubitAuthError, KioubitExpiredError, KioubitIdentity

DOMAIN = "peer.example.com"
WG_PUBKEY = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="
AUTH_RESPONSE = {"params": "cGFyYW1z", "signature": "c2ln"}
NODE_ID = "test-node"


@pytest.fixture(autouse=True)
def _clean():
    reset_state()
    yield
    reset_state()


@pytest.fixture
def client_disabled(sample_config: AppConfig, db_path: Path) -> TestClient:
    configure(config=sample_config, db_path=db_path, token="admin-tok")
    return TestClient(app)


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path, mock_wg_keypair) -> TestClient:
    configure(config=sample_config, db_path=db_path, token="admin-tok", auto_peer_domain=DOMAIN)
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        store.upsert_self(NODE_ID, name="self")
        store.apply_address_update(NODE_ID, auto_peer=True, endpoint_host="hub.example.com")
    finally:
        db.close()
    return TestClient(app)


def _identity(digest: str = "digest-1") -> KioubitIdentity:
    return KioubitIdentity(
        asn=4242421234,
        mntner="TEST-MNT",
        authtype="logincode",
        issued_at=1668266926.0,
        digest=digest,
    )


def _open_session(client: TestClient, digest: str = "digest-1") -> str:
    with patch("dn42ctl.api.verify_auth_response", return_value=_identity(digest)):
        r = client.post("/api/public/auto-peer/session", json=AUTH_RESPONSE)
    assert r.status_code == 200
    return r.json()["peer_session_token"]


def test_503_when_domain_not_configured(client_disabled: TestClient) -> None:
    r = client_disabled.post("/api/public/auto-peer/session", json=AUTH_RESPONSE)
    assert r.status_code == 503

    r = client_disabled.get("/api/public/auto-peer/nodes")
    assert r.status_code == 503

    r = client_disabled.post(
        "/api/public/auto-peer/submit",
        json={"node_id": NODE_ID, "wg_public_key": WG_PUBKEY, "peer_lla": "fe80::1"},
        headers={"Authorization": "Bearer whatever"},
    )
    assert r.status_code == 503


def test_nodes_lists_open_nodes(client: TestClient, db_path: Path) -> None:
    r = client.get("/api/public/auto-peer/nodes")
    assert r.status_code == 200
    assert r.json()["nodes"] == [{"node_id": NODE_ID, "name": "self", "endpoint_host": "hub.example.com"}]


def test_nodes_is_empty_when_no_node_is_open(client: TestClient, db_path: Path) -> None:
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).apply_address_update(NODE_ID, auto_peer=False)
    finally:
        db.close()

    r = client.get("/api/public/auto-peer/nodes")
    assert r.status_code == 200
    assert r.json()["nodes"] == []


def test_session_returns_verified_identity(client: TestClient) -> None:
    with patch("dn42ctl.api.verify_auth_response", return_value=_identity()) as verify:
        r = client.post("/api/public/auto-peer/session", json=AUTH_RESPONSE)
    assert r.status_code == 200
    data = r.json()
    assert data["verified_asn"] == 4242421234
    assert data["verified_mntner"] == "TEST-MNT"
    assert data["peer_session_token"]
    assert verify.call_args.kwargs["domain"] == DOMAIN


def test_session_rejects_empty_fields(client: TestClient) -> None:
    r = client.post("/api/public/auto-peer/session", json={"params": "", "signature": "c2ln"})
    assert r.status_code == 422


def test_session_bad_signature(client: TestClient) -> None:
    with patch("dn42ctl.api.verify_auth_response", side_effect=KioubitAuthError("签名校验失败")):
        r = client.post("/api/public/auto-peer/session", json=AUTH_RESPONSE)
    assert r.status_code == 400


def test_session_expired_response(client: TestClient) -> None:
    with patch("dn42ctl.api.verify_auth_response", side_effect=KioubitExpiredError("认证响应已过期")):
        r = client.post("/api/public/auto-peer/session", json=AUTH_RESPONSE)
    assert r.status_code == 410


def test_session_rejects_replayed_response(client: TestClient) -> None:
    _open_session(client)
    with patch("dn42ctl.api.verify_auth_response", return_value=_identity()):
        r = client.post("/api/public/auto-peer/session", json=AUTH_RESPONSE)
    assert r.status_code == 410


def test_submit_with_session(client: TestClient) -> None:
    token = _open_session(client)
    r = client.post(
        "/api/public/auto-peer/submit",
        json={
            "node_id": NODE_ID,
            "wg_public_key": WG_PUBKEY,
            "endpoint": "example.com:51820",
            "peer_lla": "fe80::1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    assert r.json()["proposal_id"]
    assert r.json()["status"] == "pending"
    assert r.json()["node_id"] == NODE_ID
    assert r.json()["node_name"] == "self"


def test_submit_requires_node_id(client: TestClient) -> None:
    token = _open_session(client)
    r = client.post(
        "/api/public/auto-peer/submit",
        json={"wg_public_key": WG_PUBKEY, "peer_lla": "fe80::1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_submit_to_a_closed_node(client: TestClient, db_path: Path) -> None:
    token = _open_session(client)
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).apply_address_update(NODE_ID, auto_peer=False)
    finally:
        db.close()

    r = client.post(
        "/api/public/auto-peer/submit",
        json={"node_id": NODE_ID, "wg_public_key": WG_PUBKEY, "peer_lla": "fe80::1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_submit_without_token(client: TestClient) -> None:
    r = client.post(
        "/api/public/auto-peer/submit",
        json={"node_id": NODE_ID, "wg_public_key": WG_PUBKEY, "peer_lla": "fe80::1"},
    )
    assert r.status_code == 401


def test_submit_expired_token(client: TestClient) -> None:
    r = client.post(
        "/api/public/auto-peer/submit",
        json={"node_id": NODE_ID, "wg_public_key": WG_PUBKEY, "peer_lla": "fe80::1"},
        headers={"Authorization": "Bearer fake-expired-token"},
    )
    assert r.status_code == 410
