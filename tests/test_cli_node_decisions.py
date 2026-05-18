from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from conftest import VALID_ENDPOINT, VALID_PEER_LLA, VALID_PUBKEY
from typer.testing import CliRunner

from dn42ctl.cli import app
from dn42ctl.config import AppConfig, save_config

NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture(autouse=True)
def _mock_wg(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from conftest import FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY

    with patch(
        "dn42ctl.services.core.generate_wg_keypair",
        return_value=(FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY),
    ), patch(
        "dn42ctl.services.bgp.generate_random_lla_cidr",
        return_value="fe80::abcd:1234/64",
    ), patch(
        "dn42ctl.services.ibgp.generate_random_lla_cidr",
        return_value="fe80::abcd:5678/64",
    ):
        yield


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def setup(
    sample_config: AppConfig, db_path: Path, tmp_path: Path
) -> tuple[list[str], AppConfig]:
    """Write a real AppConfig file so accept/import (which require_config_or_exit) works."""
    cfg_path = tmp_path / "config.toml"
    save_config(cfg_path, sample_config)
    return (["--db-path", str(db_path), "--config-path", str(cfg_path)], sample_config)


def _register_and_submit(
    runner: CliRunner, base_args: list[str], tmp_path: Path
) -> int:
    """Add node and submit a peer_add proposal via the service layer directly,
    returning the proposal id. (CLI push requires a live server fixture.)
    """
    runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
    from dn42ctl.services import submit_proposal

    # Find db_path argument
    db_path = Path(base_args[base_args.index("--db-path") + 1])
    p = submit_proposal(
        db_path=db_path,
        node_id=NODE_A,
        source="push",
        kind="peer_add",
        payload={
            "peer_kind": "bgp",
            "peer": {
                "peer_asn": 4242421234,
                "peer_public_key": VALID_PUBKEY,
                "endpoint": VALID_ENDPOINT,
                "peer_lla": VALID_PEER_LLA,
                "net_backend": "networkd",
            },
        },
    )
    return p.id


class TestAcceptProposal:
    def test_basic(self, runner: CliRunner, setup, tmp_path: Path) -> None:
        base_args, _ = setup
        pid = _register_and_submit(runner, base_args, tmp_path)
        result = runner.invoke(app, [*base_args, "node", "accept-proposal", str(pid)])
        assert result.exit_code == 0, result.output
        assert "已接受" in result.output


class TestRejectProposal:
    def test_with_reason(self, runner: CliRunner, setup, tmp_path: Path) -> None:
        base_args, _ = setup
        pid = _register_and_submit(runner, base_args, tmp_path)
        result = runner.invoke(
            app, [*base_args, "node", "reject-proposal", str(pid), "--reason", "nope"]
        )
        assert result.exit_code == 0, result.output
        assert "已拒绝" in result.output

    def test_missing_reason(self, runner: CliRunner, setup, tmp_path: Path) -> None:
        base_args, _ = setup
        pid = _register_and_submit(runner, base_args, tmp_path)
        result = runner.invoke(app, [*base_args, "node", "reject-proposal", str(pid)])
        assert result.exit_code != 0


class TestImportReport:
    def test_basic(self, runner: CliRunner, setup, tmp_path: Path) -> None:
        base_args, sample_config = setup
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        from dn42ctl.services import submit_report

        db_path = Path(base_args[base_args.index("--db-path") + 1])
        r = submit_report(
            db_path=db_path,
            node_id=NODE_A,
            kind="scan_result",
            payload={
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
        )
        result = runner.invoke(app, [*base_args, "node", "import-report", str(r.id)])
        assert result.exit_code == 0, result.output
        assert "已导入" in result.output
