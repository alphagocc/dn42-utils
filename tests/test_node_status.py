"""Stage P2.3a: get_node_status + GET /api/v1/nodes/{id}/status + CLI status."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from dn42ctl.api import app as api_app
from dn42ctl.api import configure
from dn42ctl.cli import app as cli_app
from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import (
    Dn42CtlError,
    build_desired_state,
    get_node_status,
    rollback_to,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
ADMIN_TOKEN = "admin-secret"
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
SERVER = "http://[::1]:4242"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


def _register(db_path: Path) -> None:
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).add(NODE_A, "alpha")
    finally:
        db.close()


# ---- service layer ----


class TestGetNodeStatus:
    def test_fresh_node(self, db_path: Path) -> None:
        _register(db_path)
        status = get_node_status(db_path=db_path, node_id=NODE_A)
        assert status.node_id == NODE_A
        assert status.name == "alpha"
        assert status.enabled is True
        assert status.is_self is False
        assert status.has_token is False
        assert status.last_seen_at is None
        assert status.current_revision is None
        assert status.pinned_revision is None

    def test_with_revision(self, db_path: Path) -> None:
        _register(db_path)
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        status = get_node_status(db_path=db_path, node_id=NODE_A)
        assert status.current_revision == ds.revision
        assert status.pinned_revision is None

    def test_with_pin(self, db_path: Path) -> None:
        _register(db_path)
        ds = build_desired_state(db_path=db_path, node_id=NODE_A)
        rollback_to(db_path=db_path, node_id=NODE_A, revision=ds.revision)
        status = get_node_status(db_path=db_path, node_id=NODE_A)
        assert status.pinned_revision == ds.revision

    def test_missing(self, db_path: Path) -> None:
        Database.open(db_path).close()
        with pytest.raises(Dn42CtlError, match="不存在"):
            get_node_status(db_path=db_path, node_id=NODE_A)


# ---- API layer ----


@pytest.fixture
def api_client(sample_config: AppConfig, db_path: Path) -> Iterator[TestClient]:
    configure(config=sample_config, db_path=db_path, token=ADMIN_TOKEN)
    yield TestClient(api_app)


def _register_with_token(client: TestClient, node_id: str = NODE_A) -> str:
    client.post("/api/admin/nodes", json={"node_id": node_id, "name": "x"}, headers=ADMIN_H)
    r = client.post(f"/api/admin/nodes/{node_id}/token", headers=ADMIN_H)
    return r.json()["token"]


class TestStatusRoute:
    def test_node_can_query_own(self, api_client: TestClient) -> None:
        token = _register_with_token(api_client)
        resp = api_client.get(
            f"/api/v1/nodes/{NODE_A}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["node_id"] == NODE_A
        assert body["has_token"] is True

    def test_admin_can_query_any(self, api_client: TestClient) -> None:
        _register_with_token(api_client)
        resp = api_client.get(f"/api/v1/nodes/{NODE_A}/status", headers=ADMIN_H)
        assert resp.status_code == 200

    def test_no_token(self, api_client: TestClient) -> None:
        _register_with_token(api_client)
        resp = api_client.get(f"/api/v1/nodes/{NODE_A}/status")
        assert resp.status_code == 401

    def test_node_cannot_query_other(self, api_client: TestClient) -> None:
        token_a = _register_with_token(api_client, NODE_A)
        other = "22222222-2222-4222-8222-222222222222"
        _register_with_token(api_client, other)
        resp = api_client.get(
            f"/api/v1/nodes/{other}/status",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 403

    def test_missing_node(self, api_client: TestClient) -> None:
        resp = api_client.get(f"/api/v1/nodes/{NODE_A}/status", headers=ADMIN_H)
        assert resp.status_code == 400


# ---- CLI layer (uses mock server via respx) ----


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_base_args(db_path: Path, tmp_path: Path) -> list[str]:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    return ["--db-path", str(db_path), "--config-path", str(cfg)]


@pytest.fixture
def node_toml_path(tmp_path: Path) -> Path:
    p = tmp_path / "node.toml"
    p.write_text(
        f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "spoke-token"\n',
        encoding="utf-8",
    )
    return p


class TestStatusCli:
    def test_lists_local_state(self, runner: CliRunner, cli_base_args: list[str], node_toml_path: Path) -> None:
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/status").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "node_id": NODE_A,
                        "name": "alpha",
                        "enabled": True,
                        "is_self": False,
                        "has_token": True,
                        "last_seen_at": "2026-05-19T00:00:00+00:00",
                        "current_revision": "r1",
                        "pinned_revision": None,
                    },
                )
            )
            result = runner.invoke(
                cli_app,
                [*cli_base_args, "node", "status", "--node-config-path", str(node_toml_path)],
            )
        assert result.exit_code == 0, result.output
        assert "current_revision: r1" in result.output
        assert "pinned_revision: (none)" in result.output
        assert "(没有缓存" in result.output  # no cache yet

    def test_no_node_toml(self, runner: CliRunner, cli_base_args: list[str], tmp_path: Path) -> None:
        result = runner.invoke(
            cli_app,
            [*cli_base_args, "node", "status", "--node-config-path", str(tmp_path / "nope.toml")],
        )
        assert result.exit_code != 0

    def test_server_unreachable(self, runner: CliRunner, cli_base_args: list[str], node_toml_path: Path) -> None:
        with respx.mock(base_url=SERVER) as router:
            router.get(f"/api/v1/nodes/{NODE_A}/status").mock(side_effect=httpx.ConnectError("no route"))
            result = runner.invoke(
                cli_app,
                [*cli_base_args, "node", "status", "--node-config-path", str(node_toml_path)],
            )
        assert result.exit_code != 0
        assert "不可达" in result.output
