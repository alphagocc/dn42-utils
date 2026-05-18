from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from typer.testing import CliRunner

from dn42ctl.cli import app

SERVER = "http://[::1]:4242"
NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_args(db_path: Path, tmp_path: Path) -> list[str]:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    return ["--db-path", str(db_path), "--config-path", str(cfg)]


@pytest.fixture
def node_toml_path(tmp_path: Path) -> Path:
    p = tmp_path / "node.toml"
    p.write_text(
        f'server = "{SERVER}"\nnode_id = "{NODE_A}"\ntoken = "tok"\n',
        encoding="utf-8",
    )
    return p


class TestPush:
    def test_submits_proposals(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        items = [{"kind": "peer_add", "payload": {"n": 1}}, {"kind": "peer_add", "payload": {"n": 2}}]
        json_file = tmp_path / "items.json"
        json_file.write_text(json.dumps(items), encoding="utf-8")
        with respx.mock(base_url=SERVER) as router:
            router.post(f"/api/v1/nodes/{NODE_A}/proposals").mock(
                side_effect=[
                    httpx.Response(201, json={"id": 1, "kind": "peer_add", "status": "pending"}),
                    httpx.Response(201, json={"id": 2, "kind": "peer_add", "status": "pending"}),
                ]
            )
            result = runner.invoke(
                app,
                [
                    *base_args, "node", "push", "--json", str(json_file),
                    "--node-config-path", str(node_toml_path),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "共提交 2" in result.output

    def test_bad_json_top_level(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        json_file = tmp_path / "items.json"
        json_file.write_text('{"not": "list"}', encoding="utf-8")
        result = runner.invoke(
            app,
            [
                *base_args, "node", "push", "--json", str(json_file),
                "--node-config-path", str(node_toml_path),
            ],
        )
        assert result.exit_code != 0


class TestReport:
    def test_submits_report(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        json_file = tmp_path / "payload.json"
        json_file.write_text(json.dumps({"ok": True, "revision": "rev-1"}), encoding="utf-8")
        with respx.mock(base_url=SERVER) as router:
            router.post(f"/api/v1/nodes/{NODE_A}/reports").mock(
                return_value=httpx.Response(
                    201, json={"id": 5, "kind": "apply_result", "received_at": "2026-05-19T00:00:00+00:00"}
                )
            )
            result = runner.invoke(
                app,
                [
                    *base_args, "node", "report",
                    "--kind", "apply_result",
                    "--json", str(json_file),
                    "--node-config-path", str(node_toml_path),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "#5" in result.output

    def test_payload_must_be_object(
        self, runner: CliRunner, base_args: list[str], node_toml_path: Path, tmp_path: Path
    ) -> None:
        json_file = tmp_path / "payload.json"
        json_file.write_text("[]", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                *base_args, "node", "report",
                "--kind", "apply_result",
                "--json", str(json_file),
                "--node-config-path", str(node_toml_path),
            ],
        )
        assert result.exit_code != 0


class TestAdminProposalsReports:
    def test_proposals_empty(self, runner: CliRunner, base_args: list[str]) -> None:
        # Need to register node first.
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "proposals", NODE_A])
        assert result.exit_code == 0
        assert "(没有" in result.output

    def test_reports_empty(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "reports", NODE_A])
        assert result.exit_code == 0
        assert "(没有" in result.output
