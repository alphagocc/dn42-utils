"""Stage P2.2: rotate_token / remove --force 同步 self node.toml."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.node_config import NodeConfig, load_node_config, save_node_config
from dn42ctl.services import remove_node, rotate_token
from dn42ctl.services.core import Dn42CtlError

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_SELF = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


def _register_self(db_path: Path) -> None:
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        store.upsert_self(NODE_SELF, name="self")
    finally:
        db.close()


def _seed_self_toml(toml_path: Path, *, token: str) -> None:
    save_node_config(
        toml_path,
        NodeConfig(server="http://[::1]:4242", node_id=NODE_SELF, token=token),
    )


class TestRotateTokenSelf:
    def test_self_rotate_updates_node_toml(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="stale-token")
        rotated = rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        assert rotated.self_node_toml_updated is True
        assert rotated.self_node_toml_path == toml
        # token in file matches the freshly-issued plaintext
        loaded = load_node_config(toml)
        assert loaded.token == rotated.plaintext
        assert loaded.token != "stale-token"

    def test_self_rotate_no_toml_no_crash(self, db_path: Path, tmp_path: Path) -> None:
        """Rotating self when node.toml is missing must still succeed; the DB
        hash is updated but the local file is untouched (admin will need to
        bootstrap the spoke separately).
        """
        _register_self(db_path)
        toml = tmp_path / "nope.toml"  # does NOT exist
        rotated = rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        assert rotated.self_node_toml_updated is False
        # And DB still has a fresh hash; we can verify by authenticating.
        db = Database.open(db_path)
        try:
            store = ManagedNodeStore(db.connection)
            assert store.authenticate(rotated.plaintext) is not None
        finally:
            db.close()

    def test_non_self_rotate_skips_toml(self, db_path: Path, tmp_path: Path) -> None:
        """Rotating a normal (non-self) node must not touch node.toml even if present."""
        db = Database.open(db_path)
        try:
            ManagedNodeStore(db.connection).add(NODE_A, "alpha")
        finally:
            db.close()
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="self-keeps-this")
        rotated = rotate_token(db_path=db_path, node_id=NODE_A, self_node_toml_path=toml)
        assert rotated.self_node_toml_updated is False
        # toml content not touched.
        loaded = load_node_config(toml)
        assert loaded.token == "self-keeps-this"


class TestRemoveSelf:
    def test_force_remove_clears_node_toml(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="placeholder")
        assert toml.exists()
        removed = remove_node(
            db_path=db_path, node_id=NODE_SELF, force=True, self_node_toml_path=toml,
        )
        assert removed.is_self is True
        assert not toml.exists()

    def test_remove_non_self_does_not_touch_toml(self, db_path: Path, tmp_path: Path) -> None:
        db = Database.open(db_path)
        try:
            ManagedNodeStore(db.connection).add(NODE_A, "alpha")
        finally:
            db.close()
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="unrelated")
        remove_node(
            db_path=db_path, node_id=NODE_A, force=False, self_node_toml_path=toml,
        )
        assert toml.exists()  # untouched
        assert load_node_config(toml).token == "unrelated"

    def test_self_remove_requires_force(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="placeholder")
        with pytest.raises(Dn42CtlError, match="self"):
            remove_node(
                db_path=db_path, node_id=NODE_SELF, force=False, self_node_toml_path=toml,
            )
        # toml untouched.
        assert toml.exists()
