from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher

from dn42ctl.db import Database, DatabaseError
from dn42ctl.db_managed import (
    DEFAULT_WRITE_POLICY,
    ManagedNodeStore,
    hash_token,
    validate_write_policy,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NODE_SELF = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Swap the module-level PasswordHasher with the cheapest legal config.

    argon2 defaults take ~50ms per hash; over many tests that's seconds. The
    parameters here are well below production strength but still exercise the
    same code paths.
    """
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def store(mem_db: Database) -> ManagedNodeStore:
    return ManagedNodeStore(mem_db.connection)


class TestValidateWritePolicy:
    def test_defaults_merged(self) -> None:
        result = validate_write_policy({})
        assert result == DEFAULT_WRITE_POLICY

    def test_override_peer_add_auto_accept(self) -> None:
        result = validate_write_policy({"peer_add": "auto_accept"})
        assert result["peer_add"] == "auto_accept"
        assert result["peer_modify"] == "review"

    def test_reject_unknown_key(self) -> None:
        with pytest.raises(ValueError, match="未知"):
            validate_write_policy({"bogus": "x"})

    def test_reject_peer_modify_auto(self) -> None:
        with pytest.raises(ValueError, match="peer_modify"):
            validate_write_policy({"peer_modify": "auto_accept"})

    def test_reject_peer_delete_auto(self) -> None:
        with pytest.raises(ValueError, match="peer_delete"):
            validate_write_policy({"peer_delete": "auto_accept"})

    def test_reject_invalid_report(self) -> None:
        with pytest.raises(ValueError, match="report"):
            validate_write_policy({"report": "lol"})


class TestManagedNodeStoreAdd:
    def test_add_basic(self, store: ManagedNodeStore) -> None:
        node = store.add(NODE_A, "alpha")
        assert node.node_id == NODE_A
        assert node.name == "alpha"
        assert node.is_self is False
        assert node.enabled is True
        assert node.api_token_hash is None
        assert node.write_policy == DEFAULT_WRITE_POLICY

    def test_add_creates_nodes_row(self, store: ManagedNodeStore, mem_db: Database) -> None:
        store.add(NODE_A, "alpha")
        row = mem_db.connection.execute("SELECT COUNT(*) FROM nodes WHERE node_id=?", (NODE_A,)).fetchone()
        assert row[0] == 1

    def test_add_duplicate(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        with pytest.raises(DatabaseError, match="已存在"):
            store.add(NODE_A, "alpha")


class TestManagedNodeStoreList:
    def test_empty(self, store: ManagedNodeStore) -> None:
        assert store.list_all() == []

    def test_self_first(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        store.upsert_self(NODE_SELF, name="self")
        nodes = store.list_all()
        assert nodes[0].is_self is True
        assert nodes[0].node_id == NODE_SELF


class TestUpsertSelf:
    def test_creates(self, store: ManagedNodeStore) -> None:
        node = store.upsert_self(NODE_SELF)
        assert node.is_self is True
        assert node.name == "self"

    def test_idempotent(self, store: ManagedNodeStore, mem_db: Database) -> None:
        store.upsert_self(NODE_SELF)
        store.upsert_self(NODE_SELF)
        count = mem_db.connection.execute(
            "SELECT COUNT(*) FROM managed_nodes WHERE node_id=?",
            (NODE_SELF,),
        ).fetchone()[0]
        assert count == 1

    def test_get_self(self, store: ManagedNodeStore) -> None:
        assert store.get_self() is None
        store.upsert_self(NODE_SELF)
        assert store.get_self() is not None


class TestDelete:
    def test_delete_normal(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        removed = store.delete(NODE_A)
        assert removed is not None
        assert removed.node_id == NODE_A
        assert store.get(NODE_A) is None

    def test_delete_missing_returns_none(self, store: ManagedNodeStore) -> None:
        assert store.delete(NODE_A) is None

    def test_delete_self_refused_without_force(self, store: ManagedNodeStore) -> None:
        store.upsert_self(NODE_SELF)
        with pytest.raises(DatabaseError, match="self"):
            store.delete(NODE_SELF)

    def test_delete_self_force(self, store: ManagedNodeStore) -> None:
        store.upsert_self(NODE_SELF)
        removed = store.delete(NODE_SELF, force=True)
        assert removed is not None
        assert store.get(NODE_SELF) is None


class TestTokens:
    def test_rotate_and_authenticate(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        store.rotate_token(NODE_A, "secrettokenvalue")
        node = store.authenticate("secrettokenvalue")
        assert node is not None
        assert node.node_id == NODE_A

    def test_authenticate_wrong_token(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        store.rotate_token(NODE_A, "secrettokenvalue")
        assert store.authenticate("wrong") is None

    def test_authenticate_no_token_set(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        # No rotate_token call -> hash is NULL -> not authenticatable.
        assert store.authenticate("anything") is None

    def test_rotate_replaces_old(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        store.rotate_token(NODE_A, "token1")
        store.rotate_token(NODE_A, "token2")
        assert store.authenticate("token1") is None
        assert store.authenticate("token2") is not None

    def test_disabled_node_not_authenticated(self, store: ManagedNodeStore, mem_db: Database) -> None:
        store.add(NODE_A, "alpha")
        store.rotate_token(NODE_A, "tok")
        mem_db.connection.execute("UPDATE managed_nodes SET enabled=0 WHERE node_id=?", (NODE_A,))
        mem_db.connection.commit()
        assert store.authenticate("tok") is None

    def test_set_token_hash_missing_node(self, store: ManagedNodeStore) -> None:
        with pytest.raises(DatabaseError, match="不存在"):
            store.set_token_hash(NODE_A, hash_token("x"))


class TestPolicy:
    def test_set_full(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        updated = store.set_write_policy(
            NODE_A,
            {"peer_add": "auto_accept", "report": "review"},
        )
        assert updated.write_policy["peer_add"] == "auto_accept"
        assert updated.write_policy["report"] == "review"
        # unchanged keys preserved
        assert updated.write_policy["peer_modify"] == "review"

    def test_set_invalid_value(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        with pytest.raises(ValueError):
            store.set_write_policy(NODE_A, {"peer_add": "bogus"})

    def test_set_missing_node(self, store: ManagedNodeStore) -> None:
        with pytest.raises(DatabaseError, match="不存在"):
            store.set_write_policy(NODE_A, {})


class TestTouchLastSeen:
    def test_updates(self, store: ManagedNodeStore, mem_db: Database) -> None:
        store.add(NODE_A, "alpha")
        assert store.get(NODE_A).last_seen_at is None
        store.touch_last_seen(NODE_A)
        node = store.get(NODE_A)
        assert node is not None
        assert node.last_seen_at is not None


class TestForeignKeyCascade:
    def test_cascade_via_nodes_delete(self, store: ManagedNodeStore, mem_db: Database) -> None:
        store.add(NODE_A, "alpha")
        # Inserting a proposal referencing this node should succeed.
        mem_db.connection.execute(
            "INSERT INTO config_proposals(node_id,source,kind,payload_json,received_at) VALUES (?,?,?,?,?)",
            (NODE_A, "push", "peer_add", "{}", "2026-05-18T00:00:00+00:00"),
        )
        mem_db.connection.commit()
        # Deleting via parent nodes table cascades to managed_nodes and proposals.
        mem_db.connection.execute("DELETE FROM nodes WHERE node_id=?", (NODE_A,))
        mem_db.connection.commit()
        assert store.get(NODE_A) is None
        cnt = mem_db.connection.execute("SELECT COUNT(*) FROM config_proposals").fetchone()[0]
        assert cnt == 0


class TestDefaultWritePolicyInDb:
    def test_default_persisted(self, store: ManagedNodeStore, mem_db: Database) -> None:
        store.add(NODE_A, "alpha")
        row = mem_db.connection.execute(
            "SELECT write_policy FROM managed_nodes WHERE node_id=?",
            (NODE_A,),
        ).fetchone()
        assert json.loads(row[0]) == DEFAULT_WRITE_POLICY
