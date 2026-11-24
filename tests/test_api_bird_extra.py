from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import dn42ctl.api as api
from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore, hash_token
from dn42ctl.services.bird_extra import BirdExtraError

SELF = "11111111-1111-4111-8111-111111111111"
REMOTE = "22222222-2222-4222-8222-222222222222"
URL = "/api/admin/bird/extra-conf"
ADMIN = {"Authorization": "Bearer admin-token"}
NODE = {"Authorization": "Bearer node-token"}


@pytest.fixture
def client(sample_config: AppConfig, db_path: Path) -> TestClient:
    api.configure(config=sample_config, db_path=db_path, token="admin-token")
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        store.upsert_self(SELF)
        store.add(REMOTE, "remote")
        store.rotate_token(SELF, "self-token")
        store.rotate_token(REMOTE, "node-token")
    finally:
        db.close()
    return TestClient(api.app)


def test_admin_reads_and_edits_selected_node(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    rpc = AsyncMock(return_value={"content": "", "revision": "missing"})
    monkeypatch.setattr(api._registry, "request_bird_extra", rpc)
    read = client.get(URL, headers=ADMIN)
    assert read.status_code == 200
    assert read.json() == {"content": "", "revision": "missing"}
    assert read.headers["Cache-Control"] == "no-store"
    rpc.assert_awaited_once_with(SELF, token_hash=hash_token("self-token"))

    body = {"content": "# remote\n", "expected_revision": "missing"}
    rpc.side_effect = BirdExtraError(409, "文件已修改")
    response = client.put(URL, params={"node_id": REMOTE}, json=body, headers=ADMIN)
    assert response.status_code == 409
    rpc.assert_awaited_with(REMOTE, token_hash=hash_token("node-token"), content=body["content"], revision="missing")
    rpc.reset_mock()
    rejected = client.put(URL, json={**body, "path": "/other/file"}, headers=ADMIN)
    assert rejected.status_code == 422
    rpc.assert_not_awaited()


def test_offline_disabled_and_unauthorized(client: TestClient, db_path: Path) -> None:
    assert client.get(URL).status_code == 401
    params = {"node_id": REMOTE}
    assert client.get(URL, params=params, headers=NODE).status_code == 403
    assert (
        client.put(URL, params=params, json={"content": "", "expected_revision": "missing"}, headers=NODE).status_code
        == 403
    )
    assert client.get(URL, params=params, headers=ADMIN).status_code == 503
    assert client.get(URL, params={"node_id": "unknown"}, headers=ADMIN).status_code == 404
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).set_enabled(REMOTE, False)
    finally:
        db.close()
    assert client.get(URL, params=params, headers=ADMIN).status_code == 403
