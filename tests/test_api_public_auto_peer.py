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


def _config_with_registry(sample_config: AppConfig, registry: Path) -> AppConfig:
    return AppConfig(
        **{
            **{f.name: getattr(sample_config, f.name) for f in sample_config.__dataclass_fields__.values()},
            "dn42_registry_path": str(registry),
        }
    )


@pytest.fixture(autouse=True)
def _clean():
    reset_state()
    yield
    reset_state()


@pytest.fixture
def client_no_registry(sample_config: AppConfig, db_path: Path) -> TestClient:
    configure(config=sample_config, db_path=db_path, token="admin-tok")
    return TestClient(app)


@pytest.fixture
def client(sample_config: AppConfig, dn42_registry: Path, db_path: Path, mock_wg_keypair) -> TestClient:
    cfg = _config_with_registry(sample_config, dn42_registry)
    configure(config=cfg, db_path=db_path, token="admin-tok")
    # bootstrap self node
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).upsert_self("test-node", name="self")
    finally:
        db.close()
    return TestClient(app)


def test_503_when_no_registry(client_no_registry: TestClient) -> None:
    r = client_no_registry.post("/api/public/auto-peer/lookup", json={"asn": 1234})
    assert r.status_code == 503


def test_lookup(client: TestClient) -> None:
    r = client.post("/api/public/auto-peer/lookup", json={"asn": 4242421234})
    assert r.status_code == 200
    data = r.json()
    assert data["asn"] == 4242421234
    assert len(data["mntners"]) == 2


def test_lookup_unknown_asn(client: TestClient) -> None:
    r = client.post("/api/public/auto-peer/lookup", json={"asn": 9999999999})
    assert r.status_code == 404


def test_challenge_and_verify_and_submit(client: TestClient) -> None:
    # step 1: lookup
    r = client.post("/api/public/auto-peer/lookup", json={"asn": 4242421234})
    assert r.status_code == 200

    # step 2: challenge
    r = client.post(
        "/api/public/auto-peer/challenge",
        json={"asn": 4242421234, "mntner": "TEST-MNT", "auth_index": 0},
    )
    assert r.status_code == 200
    cid = r.json()["challenge_id"]
    assert r.json()["scheme"] == "ssh"

    # step 3: verify (mock ssh verification)
    with patch("dn42ctl.services.auto_peer.verify_ssh", return_value=True):
        r = client.post(
            "/api/public/auto-peer/verify",
            json={"challenge_id": cid, "signature": "fake-valid-sig"},
        )
    assert r.status_code == 200
    token = r.json()["peer_session_token"]
    assert r.json()["verified_asn"] == 4242421234

    # step 4: submit
    r = client.post(
        "/api/public/auto-peer/submit",
        json={
            "wg_public_key": "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=",
            "endpoint": "example.com:51820",
            "peer_lla": "fe80::1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    assert r.json()["proposal_id"]
    assert r.json()["status"] == "pending"


def test_verify_bad_sig(client: TestClient) -> None:
    r = client.post(
        "/api/public/auto-peer/challenge",
        json={"asn": 4242421234, "mntner": "TEST-MNT", "auth_index": 0},
    )
    cid = r.json()["challenge_id"]
    with patch("dn42ctl.services.auto_peer.verify_ssh", return_value=False):
        r = client.post(
            "/api/public/auto-peer/verify",
            json={"challenge_id": cid, "signature": "bad"},
        )
    assert r.status_code == 400


def test_submit_without_token(client: TestClient) -> None:
    r = client.post(
        "/api/public/auto-peer/submit",
        json={
            "wg_public_key": "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=",
            "peer_lla": "fe80::1",
        },
    )
    assert r.status_code == 401


def test_submit_expired_token(client: TestClient) -> None:
    r = client.post(
        "/api/public/auto-peer/submit",
        json={
            "wg_public_key": "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=",
            "peer_lla": "fe80::1",
        },
        headers={"Authorization": "Bearer fake-expired-token"},
    )
    assert r.status_code == 410
