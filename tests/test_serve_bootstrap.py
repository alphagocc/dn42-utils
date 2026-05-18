from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.node_config import load_node_config
from dn42ctl.serve_bootstrap import run_self_registration


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "dn42.sqlite3",
        tmp_path / "self_node_id",
        tmp_path / "node.toml",
    )


class TestRunSelfRegistration:
    def test_first_run_creates_everything(self, tmp_path: Path) -> None:
        db, sid, ncfg = _paths(tmp_path)
        result = run_self_registration(
            db_path=db, self_node_id_path=sid, node_toml_path=ncfg,
            server_url="http://[::1]:4242",
        )
        assert result.created_node_id is True
        assert result.rotated_token is True
        assert sid.exists()
        assert ncfg.exists()
        cfg = load_node_config(ncfg)
        assert cfg.server == "http://[::1]:4242"
        assert cfg.node_id == result.node_id
        assert cfg.token  # non-empty

    def test_idempotent(self, tmp_path: Path) -> None:
        db, sid, ncfg = _paths(tmp_path)
        r1 = run_self_registration(db_path=db, self_node_id_path=sid, node_toml_path=ncfg)
        token_before = load_node_config(ncfg).token
        r2 = run_self_registration(db_path=db, self_node_id_path=sid, node_toml_path=ncfg)
        assert r1.node_id == r2.node_id
        assert r2.created_node_id is False
        assert r2.rotated_token is False
        token_after = load_node_config(ncfg).token
        assert token_before == token_after

    def test_managed_nodes_row_is_self(self, tmp_path: Path) -> None:
        db, sid, ncfg = _paths(tmp_path)
        run_self_registration(db_path=db, self_node_id_path=sid, node_toml_path=ncfg)
        d = Database.open(db)
        try:
            store = ManagedNodeStore(d.connection)
            node = store.get_self()
        finally:
            d.close()
        assert node is not None
        assert node.is_self is True
        assert node.api_token_hash is not None

    def test_token_in_node_toml_authenticates(self, tmp_path: Path) -> None:
        db, sid, ncfg = _paths(tmp_path)
        run_self_registration(db_path=db, self_node_id_path=sid, node_toml_path=ncfg)
        cfg = load_node_config(ncfg)
        d = Database.open(db)
        try:
            node = ManagedNodeStore(d.connection).authenticate(cfg.token)
        finally:
            d.close()
        assert node is not None
        assert node.node_id == cfg.node_id

    def test_regenerates_when_node_toml_node_id_mismatch(self, tmp_path: Path) -> None:
        db, sid, ncfg = _paths(tmp_path)
        # Write a stale node.toml first.
        ncfg.write_text(
            'server = "http://[::1]:4242"\nnode_id = "00000000-0000-4000-8000-000000000000"\n'
            'token = "ancient"\n',
            encoding="utf-8",
        )
        result = run_self_registration(db_path=db, self_node_id_path=sid, node_toml_path=ncfg)
        assert result.rotated_token is True
        cfg = load_node_config(ncfg)
        assert cfg.node_id == result.node_id
        assert cfg.token != "ancient"

    def test_preserves_existing_self_node_id(self, tmp_path: Path) -> None:
        db, sid, ncfg = _paths(tmp_path)
        sid.write_text("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\n", encoding="utf-8")
        result = run_self_registration(db_path=db, self_node_id_path=sid, node_toml_path=ncfg)
        assert result.node_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert result.created_node_id is False
