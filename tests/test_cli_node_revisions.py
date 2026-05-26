from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from typer.testing import CliRunner

from dn42ctl.cli import app
from dn42ctl.db import BgpPeerRecord, Database

NODE_A = "11111111-1111-4111-8111-111111111111"
FAKE_PRIV = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_PUB = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="


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


def _gen_revisions(db_path: Path) -> tuple[str, str]:
    from dn42ctl.services import build_desired_state

    _seed_peer(db_path, 4242421111)
    r1 = build_desired_state(db_path=db_path, node_id=NODE_A).revision
    _seed_peer(db_path, 4242422222)
    r2 = build_desired_state(db_path=db_path, node_id=NODE_A).revision
    return r1, r2


class TestRevisionsCmd:
    def test_lists(self, runner: CliRunner, base_args: list[str], db_path: Path) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        r1, r2 = _gen_revisions(db_path)
        result = runner.invoke(app, [*base_args, "node", "revisions", NODE_A])
        assert result.exit_code == 0, result.output
        assert r1 in result.output
        assert r2 in result.output
        assert "pinned: (none" in result.output


class TestRollbackCmd:
    def test_pin_and_clear(self, runner: CliRunner, base_args: list[str], db_path: Path) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        r1, _ = _gen_revisions(db_path)
        result = runner.invoke(app, [*base_args, "node", "rollback", NODE_A, "--to", r1])
        assert result.exit_code == 0, result.output
        assert "已 pin" in result.output

        listing = runner.invoke(app, [*base_args, "node", "revisions", NODE_A]).output
        assert "pinned:" in listing
        assert r1 in listing

        clear = runner.invoke(app, [*base_args, "node", "rollback-clear", NODE_A])
        assert clear.exit_code == 0
        assert "已清除" in clear.output

    def test_rollback_unknown(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "rollback", NODE_A, "--to", "no-such"])
        assert result.exit_code != 0
