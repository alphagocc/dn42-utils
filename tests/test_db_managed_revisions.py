from __future__ import annotations

import pytest

from dn42ctl.db import Database, DatabaseError
from dn42ctl.db_managed import ManagedNodeStore, RevisionStore

NODE_A = "11111111-1111-4111-8111-111111111111"


def _store(mem_db: Database) -> RevisionStore:
    ManagedNodeStore(mem_db.connection).add(NODE_A, "alpha")
    return RevisionStore(mem_db.connection)


class TestRecord:
    def test_basic(self, mem_db: Database) -> None:
        store = _store(mem_db)
        rev = store.record(
            node_id=NODE_A, revision="r1", generated_at="2026-05-19T00:00:00+00:00",
            payload={"hello": "world"},
        )
        assert rev.revision == "r1"
        assert rev.payload == {"hello": "world"}

    def test_duplicate_is_idempotent(self, mem_db: Database) -> None:
        store = _store(mem_db)
        r1 = store.record(node_id=NODE_A, revision="r1", generated_at="t", payload={"a": 1})
        r2 = store.record(node_id=NODE_A, revision="r1", generated_at="t", payload={"a": 1})
        assert r1.id == r2.id

    def test_list(self, mem_db: Database) -> None:
        store = _store(mem_db)
        store.record(node_id=NODE_A, revision="r1", generated_at="t1", payload={})
        store.record(node_id=NODE_A, revision="r2", generated_at="t2", payload={})
        rows = store.list_for_node(NODE_A)
        assert [r.revision for r in rows] == ["r2", "r1"]  # newest first


class TestTrim:
    def test_keeps_latest(self, mem_db: Database) -> None:
        store = _store(mem_db)
        for i in range(10):
            store.record(node_id=NODE_A, revision=f"r{i}", generated_at=f"t{i}", payload={})
        deleted = store.trim(NODE_A, keep_latest=3)
        assert deleted == 7
        remaining = store.list_for_node(NODE_A)
        assert [r.revision for r in remaining] == ["r9", "r8", "r7"]

    def test_preserves_pinned_revision(self, mem_db: Database) -> None:
        """Pinned revision must survive trim even when it falls outside the recency window."""
        store = _store(mem_db)
        store.record(node_id=NODE_A, revision="r-pinned", generated_at="t0", payload={})
        # Pin the oldest revision.
        store.pin(NODE_A, "r-pinned")
        # Add more revisions to push r-pinned out of the recency window.
        for i in range(1, 10):
            store.record(node_id=NODE_A, revision=f"r{i}", generated_at=f"t{i}", payload={})
        store.trim(NODE_A, keep_latest=3)
        remaining_revisions = {r.revision for r in store.list_for_node(NODE_A, limit=100)}
        assert "r-pinned" in remaining_revisions
        # The latest 3 are still there.
        assert {"r9", "r8", "r7"}.issubset(remaining_revisions)
        # Get_pin still returns a row (not None).
        pin = store.get_pin(NODE_A)
        assert pin is not None
        assert pin.revision == "r-pinned"


class TestPin:
    def test_pin_and_get(self, mem_db: Database) -> None:
        store = _store(mem_db)
        store.record(node_id=NODE_A, revision="r1", generated_at="t1", payload={"v": 1})
        store.record(node_id=NODE_A, revision="r2", generated_at="t2", payload={"v": 2})
        store.pin(NODE_A, "r1")
        pinned = store.get_pin(NODE_A)
        assert pinned is not None
        assert pinned.revision == "r1"
        assert pinned.payload["v"] == 1

    def test_unpin(self, mem_db: Database) -> None:
        store = _store(mem_db)
        store.record(node_id=NODE_A, revision="r1", generated_at="t1", payload={})
        store.pin(NODE_A, "r1")
        store.unpin(NODE_A)
        assert store.get_pin(NODE_A) is None

    def test_pin_replaces_existing(self, mem_db: Database) -> None:
        store = _store(mem_db)
        store.record(node_id=NODE_A, revision="r1", generated_at="t1", payload={})
        store.record(node_id=NODE_A, revision="r2", generated_at="t2", payload={})
        store.pin(NODE_A, "r1")
        store.pin(NODE_A, "r2")
        assert store.get_pin(NODE_A).revision == "r2"

    def test_pin_missing_revision(self, mem_db: Database) -> None:
        store = _store(mem_db)
        with pytest.raises(DatabaseError, match="不存在"):
            store.pin(NODE_A, "no-such")

    def test_no_pin_returns_none(self, mem_db: Database) -> None:
        store = _store(mem_db)
        assert store.get_pin(NODE_A) is None
