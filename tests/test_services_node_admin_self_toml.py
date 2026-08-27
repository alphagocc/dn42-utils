"""Stage P2.2: rotate_token / remove --force 同步 self node.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.node_config import NodeConfig, load_node_config, save_node_config
from dn42ctl.services import remove_node, rotate_token
from dn42ctl.services.core import Dn42CtlError

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_SELF = "33333333-3333-4333-8333-333333333333"


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


def _authenticates(db_path: Path, token: str) -> bool:
    db = Database.open(db_path)
    try:
        return ManagedNodeStore(db.connection).authenticate(token) is not None
    finally:
        db.close()


class TestRotateTokenSelf:
    def test_self_rotate_updates_node_toml(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="stale-token")
        rotated = rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        assert rotated.self_node_toml_updated is True
        assert rotated.self_node_toml_path == toml
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
        loaded = load_node_config(toml)
        assert loaded.token == "self-keeps-this"


class TestRemoveSelf:
    def test_force_remove_clears_node_toml(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="placeholder")
        assert toml.exists()
        removed = remove_node(
            db_path=db_path,
            node_id=NODE_SELF,
            force=True,
            self_node_toml_path=toml,
        )
        assert removed.node.is_self is True
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
            db_path=db_path,
            node_id=NODE_A,
            force=False,
            self_node_toml_path=toml,
        )
        assert toml.exists()  # untouched
        assert load_node_config(toml).token == "unrelated"

    def test_self_remove_requires_force(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="placeholder")
        with pytest.raises(Dn42CtlError, match="self"):
            remove_node(
                db_path=db_path,
                node_id=NODE_SELF,
                force=False,
                self_node_toml_path=toml,
            )
        assert toml.exists()


class TestSelfTomlFailureIsReported:
    """node.toml 读写失败在标准部署下是常态:hub 跑在非 root 用户下,文件是 root:0600。

    此时 DB 里的 hash 已经换掉,回滚会把只出现一次的明文丢掉,所以只能报告不能中止;
    但也绝不能沉默——沉默等于把 hub 自己的 agent 静默锁在门外。见 docs/commands/node.md。
    """

    def test_rotate_reports_unreadable_toml(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="stale-token")
        toml.chmod(0o000)
        try:
            rotated = rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        finally:
            toml.chmod(0o600)

        assert rotated.self_node_toml_updated is False
        assert rotated.self_node_toml_error is not None
        assert _authenticates(db_path, rotated.plaintext)

    def test_rotate_reports_unwritable_toml(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="stale-token")
        toml.chmod(0o400)
        try:
            rotated = rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        finally:
            toml.chmod(0o600)

        assert rotated.self_node_toml_updated is False
        assert "写入 node.toml 失败" in (rotated.self_node_toml_error or "")
        assert load_node_config(toml).token == "stale-token"
        assert _authenticates(db_path, rotated.plaintext)

    def test_force_remove_reports_failed_unlink(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        holder = tmp_path / "locked"
        holder.mkdir()
        toml = holder / "node.toml"
        _seed_self_toml(toml, token="placeholder")
        holder.chmod(0o500)
        try:
            removed = remove_node(db_path=db_path, node_id=NODE_SELF, force=True, self_node_toml_path=toml)
        finally:
            holder.chmod(0o700)

        assert removed.node.is_self is True
        assert "删除 node.toml 失败" in (removed.self_node_toml_error or "")
        assert toml.exists()

    def test_successful_paths_carry_no_error(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        _seed_self_toml(toml, token="stale-token")
        assert rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml).self_node_toml_error is None
        removed = remove_node(db_path=db_path, node_id=NODE_SELF, force=True, self_node_toml_path=toml)
        assert removed.self_node_toml_error is None


class TestRotatePreservesAllFields:
    """重签 token 时不能顺手把 node.toml 的其它字段重置回默认值。

    以前 _rewrite_self_node_toml 逐字段重建 NodeConfig，凡是忘了列举的字段都会被
    静默丢掉——agent 与 reload_policy 就是这么被重置的。现在用 dataclasses.replace。
    """

    def test_preserves_reload_policy(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        save_node_config(
            toml,
            NodeConfig(
                server="http://[::1]:4242",
                node_id=NODE_SELF,
                token="stale",
                reload_policy="never",
            ),
        )
        rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        assert load_node_config(toml).reload_policy == "never"

    def test_preserves_agent_options(self, db_path: Path, tmp_path: Path) -> None:
        from dn42ctl.node_config import AgentOptions

        _register_self(db_path)
        toml = tmp_path / "node.toml"
        tuned = AgentOptions(reconnect_max_seconds=17.0, heartbeat_interval_seconds=42.0)
        save_node_config(
            toml,
            NodeConfig(server="http://[::1]:4242", node_id=NODE_SELF, token="stale", agent=tuned),
        )
        rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        assert load_node_config(toml).agent == tuned

    def test_preserves_apply_overrides_and_cache_path(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        cache = tmp_path / "custom-cache.sqlite3"
        save_node_config(
            toml,
            NodeConfig(
                server="http://[::1]:4242",
                node_id=NODE_SELF,
                token="stale",
                apply_overrides={"peers_dir": "/etc/bird/peers"},
                cache_db_path=cache,
            ),
        )
        rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        loaded = load_node_config(toml)
        assert loaded.apply_overrides == {"peers_dir": "/etc/bird/peers"}
        assert loaded.cache_db_path == cache

    def test_still_updates_token_and_node_id(self, db_path: Path, tmp_path: Path) -> None:
        _register_self(db_path)
        toml = tmp_path / "node.toml"
        save_node_config(
            toml,
            NodeConfig(server="http://[::1]:4242", node_id="old-id", token="stale", reload_policy="never"),
        )
        rotated = rotate_token(db_path=db_path, node_id=NODE_SELF, self_node_toml_path=toml)
        loaded = load_node_config(toml)
        assert loaded.token == rotated.plaintext
        assert loaded.node_id == NODE_SELF
