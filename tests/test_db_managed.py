from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dn42ctl.db import Database, DatabaseError
from dn42ctl.db_managed import (
    DEFAULT_WRITE_POLICY,
    ManagedNodeStore,
    hash_token,
    validate_write_policy,
    verify_token,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NODE_SELF = "33333333-3333-4333-8333-333333333333"


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

    def test_rename_survives_later_upserts(self, store: ManagedNodeStore) -> None:
        """每次 serve 启动都会 upsert 一次,改过的名字不能被默认值盖回去。"""
        store.upsert_self(NODE_SELF)
        store.set_name(NODE_SELF, "Frankfurt")

        assert store.upsert_self(NODE_SELF).name == "Frankfurt"

    def test_get_self(self, store: ManagedNodeStore) -> None:
        assert store.get_self() is None
        store.upsert_self(NODE_SELF)
        assert store.get_self() is not None

    def test_new_self_demotes_previous(self, store: ManagedNodeStore, mem_db: Database) -> None:
        """self_node_id 丢失后会生成新 UUID;旧行不清零就会留下两个 self。"""
        store.upsert_self(NODE_A)
        store.upsert_self(NODE_SELF)

        flagged = [r[0] for r in mem_db.connection.execute("SELECT node_id FROM managed_nodes WHERE is_self=1")]
        assert flagged == [NODE_SELF]
        assert store.get_self().node_id == NODE_SELF

    def test_demoted_row_survives_as_normal_node(self, store: ManagedNodeStore) -> None:
        """旧行降级保留:旧分区里的 peer 全都还在,删掉等于丢配置。"""
        store.upsert_self(NODE_A)
        store.upsert_self(NODE_SELF)

        demoted = store.get(NODE_A)
        assert demoted is not None
        assert demoted.is_self is False
        assert demoted.enabled is True


class TestDelete:
    def test_delete_normal(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        removed = store.delete(NODE_A)
        assert removed is not None
        assert removed.node_id == NODE_A
        assert store.get(NODE_A) is None

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

    def test_hash_is_prefixed_and_deterministic(self) -> None:
        """前缀是 v11 迁移用来识别旧格式的唯一依据。"""
        assert hash_token("tok").startswith("sha256$")
        assert hash_token("tok") == hash_token("tok")
        assert hash_token("tok") != hash_token("tok ")

    def test_legacy_hash_never_verifies(self) -> None:
        """迁移漏掉某行时必须认证失败并返回 401,异常泄漏会把它变成 500。"""
        assert verify_token("$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$xxxx", "tok") is False


class TestMigrationV12:
    def test_keeps_only_the_most_recent_self(self, tmp_path: Path) -> None:
        """1.2 -> 1.3 的中间态(v12 刚执行完、v13 的索引还没建)。
        库里可能已经有两行 is_self=1,迁移必须合并为一行且不删数据。

        按真实升级顺序构造:重复行先存在,v12 合并,v13 的唯一索引最后建。
        """
        db_path = tmp_path / "multiself.sqlite3"
        db = Database.open(db_path)
        try:
            store = ManagedNodeStore(db.connection)
            store.add(NODE_A, "stale-self")
            store.add(NODE_B, "current-self")
            db.connection.execute("DROP INDEX IF EXISTS idx_managed_nodes_single_self")
            db.connection.execute(
                "UPDATE managed_nodes SET is_self=1, updated_at=? WHERE node_id=?",
                ("2020-01-01T00:00:00+00:00", NODE_A),
            )
            db.connection.execute(
                "UPDATE managed_nodes SET is_self=1, updated_at=? WHERE node_id=?",
                ("2026-08-27T00:00:00+00:00", NODE_B),
            )
            db.connection.execute("DELETE FROM schema_migrations WHERE version IN (12, 13)")
            db.connection.commit()

            db.migrate()

            assert store.get(NODE_A).is_self is False
            assert store.get(NODE_B).is_self is True
            assert store.get(NODE_A) is not None  # 降级,不是删除
        finally:
            db.close()


class TestMigrationV13:
    def test_schema_rejects_a_second_self(self, mem_db: Database) -> None:
        """应用逻辑之外还得有一道:直接写 SQL 也不能造出两个 self。"""
        store = ManagedNodeStore(mem_db.connection)
        store.upsert_self(NODE_SELF)
        store.add(NODE_A, "alpha")
        with pytest.raises(sqlite3.IntegrityError):
            mem_db.connection.execute("UPDATE managed_nodes SET is_self=1 WHERE node_id=?", (NODE_A,))

    def test_many_non_self_rows_are_fine(self, mem_db: Database) -> None:
        """partial index 只覆盖 is_self=1,不能把普通节点也限成一行。"""
        store = ManagedNodeStore(mem_db.connection)
        store.upsert_self(NODE_SELF)
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        assert len(store.list_all()) == 3


class TestMigrationV11:
    def test_nulls_legacy_hashes_and_keeps_new_ones(self, tmp_path: Path) -> None:
        """旧格式哈希不可逆,只能作废让管理员重签;新格式的行不能被误伤。"""
        db_path = tmp_path / "legacy.sqlite3"
        db = Database.open(db_path)
        try:
            store = ManagedNodeStore(db.connection)
            store.add(NODE_A, "legacy")
            store.add(NODE_B, "current")
            db.connection.execute(
                "UPDATE managed_nodes SET api_token_hash=? WHERE node_id=?",
                ("$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$xxxx", NODE_A),
            )
            store.rotate_token(NODE_B, "fresh-token")
            db.connection.execute("DELETE FROM schema_migrations WHERE version=11")
            db.connection.commit()

            db.migrate()

            assert store.get(NODE_A).api_token_hash is None
            assert store.authenticate("fresh-token") is not None
        finally:
            db.close()


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


class TestNodeAddresses:
    def test_defaults_are_null(self, store: ManagedNodeStore) -> None:
        node = store.add(NODE_A, "alpha")
        assert node.endpoint_host is None
        assert node.own_ipv6 is None
        assert node.router_id is None

    def test_set_addresses_partial(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        node = store.set_addresses(NODE_A, own_ipv6="fd42:4242:1::1")
        assert node.own_ipv6 == "fd42:4242:1::1"
        # 未传的列保持不变
        assert node.endpoint_host is None
        assert node.router_id is None

    def test_set_addresses_explicit_none_clears(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        store.set_addresses(NODE_A, endpoint_host="a.example.com", own_ipv6="fd42:4242:1::1")
        node = store.set_addresses(NODE_A, endpoint_host=None)
        assert node.endpoint_host is None
        assert node.own_ipv6 == "fd42:4242:1::1"

    def test_set_addresses_noop_when_all_unset(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        node = store.set_addresses(NODE_A)
        assert node.node_id == NODE_A

    def test_set_addresses_unknown_node_raises(self, store: ManagedNodeStore) -> None:
        with pytest.raises(DatabaseError, match="节点不存在"):
            store.set_addresses(NODE_A, own_ipv6="fd42:4242:1::1")

    def test_set_name(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        assert store.set_name(NODE_A, "renamed").name == "renamed"

    def test_set_enabled(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        assert store.set_enabled(NODE_A, enabled=False).enabled is False
        assert store.set_enabled(NODE_A, enabled=True).enabled is True


class TestAutoPeer:
    def test_default_is_closed(self, store: ManagedNodeStore) -> None:
        assert store.add(NODE_A, "alpha").auto_peer is False

    def test_apply_address_update_toggles(self, store: ManagedNodeStore) -> None:
        store.add(NODE_A, "alpha")
        assert store.apply_address_update(NODE_A, auto_peer=True).auto_peer is True
        assert store.apply_address_update(NODE_A, auto_peer=False).auto_peer is False

    def test_list_auto_peer_needs_both_flags(self, store: ManagedNodeStore) -> None:
        """禁用一个节点同时关掉它的 auto-peer 入口,无需再记得改第二个开关。"""
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        store.apply_address_update(NODE_A, auto_peer=True)

        assert [n.node_id for n in store.list_auto_peer()] == [NODE_A]

        store.set_enabled(NODE_A, enabled=False)
        assert store.list_auto_peer() == []


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
