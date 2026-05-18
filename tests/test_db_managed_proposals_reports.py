from __future__ import annotations

from collections.abc import Iterator

import pytest
from argon2 import PasswordHasher

from dn42ctl.db import Database, DatabaseError
from dn42ctl.db_managed import (
    ManagedNodeStore,
    ProposalStore,
    ReportStore,
)

NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture
def stores(mem_db: Database) -> tuple[ProposalStore, ReportStore, ManagedNodeStore]:
    mn = ManagedNodeStore(mem_db.connection)
    mn.add(NODE_A, "alpha")
    return (ProposalStore(mem_db.connection), ReportStore(mem_db.connection), mn)


class TestProposalStore:
    def test_add_pending(self, stores) -> None:
        ps, _, _ = stores
        p = ps.add(node_id=NODE_A, source="push", kind="peer_add", payload={"asn": 1})
        assert p.status == "pending"
        assert p.kind == "peer_add"
        assert p.payload == {"asn": 1}

    def test_add_invalid_source(self, stores) -> None:
        ps, _, _ = stores
        with pytest.raises(DatabaseError, match="source"):
            ps.add(node_id=NODE_A, source="bogus", kind="peer_add", payload={})

    def test_add_invalid_kind(self, stores) -> None:
        ps, _, _ = stores
        with pytest.raises(DatabaseError, match="kind"):
            ps.add(node_id=NODE_A, source="push", kind="weird", payload={})

    def test_list_filter_status(self, stores) -> None:
        ps, _, _ = stores
        a = ps.add(node_id=NODE_A, source="push", kind="peer_add", payload={"n": 1})
        b = ps.add(node_id=NODE_A, source="push", kind="peer_add", payload={"n": 2})
        ps.set_status(a.id, "accepted")
        pending = ps.list_for_node(NODE_A, status="pending")
        assert [p.id for p in pending] == [b.id]

    def test_set_status_accepted(self, stores) -> None:
        ps, _, _ = stores
        p = ps.add(node_id=NODE_A, source="push", kind="peer_add", payload={})
        updated = ps.set_status(p.id, "accepted")
        assert updated.status == "accepted"
        assert updated.decided_at is not None

    def test_set_status_rejected_with_message(self, stores) -> None:
        ps, _, _ = stores
        p = ps.add(node_id=NODE_A, source="push", kind="peer_add", payload={})
        updated = ps.set_status(p.id, "rejected", message="冲突")
        assert updated.status == "rejected"
        assert updated.message == "冲突"

    def test_set_status_missing(self, stores) -> None:
        ps, _, _ = stores
        with pytest.raises(DatabaseError, match="不存在"):
            ps.set_status(9999, "accepted")


class TestReportStore:
    def test_add_kinds(self, stores) -> None:
        _, rs, _ = stores
        r = rs.add(node_id=NODE_A, kind="apply_result", payload={"ok": True})
        assert r.kind == "apply_result"
        assert r.payload["ok"] is True
        assert r.imported_at is None

    def test_invalid_kind(self, stores) -> None:
        _, rs, _ = stores
        with pytest.raises(DatabaseError, match="kind"):
            rs.add(node_id=NODE_A, kind="weird", payload={})

    def test_list_filter_kind(self, stores) -> None:
        _, rs, _ = stores
        rs.add(node_id=NODE_A, kind="apply_result", payload={})
        rs.add(node_id=NODE_A, kind="scan_result", payload={})
        rs.add(node_id=NODE_A, kind="apply_result", payload={})
        only_apply = rs.list_for_node(NODE_A, kind="apply_result")
        assert len(only_apply) == 2
        assert all(r.kind == "apply_result" for r in only_apply)

    def test_mark_imported(self, stores) -> None:
        _, rs, _ = stores
        r = rs.add(node_id=NODE_A, kind="scan_result", payload={})
        marked = rs.mark_imported(r.id)
        assert marked.imported_at is not None

    def test_mark_imported_missing(self, stores) -> None:
        _, rs, _ = stores
        with pytest.raises(DatabaseError, match="不存在"):
            rs.mark_imported(9999)
