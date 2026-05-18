from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import (
    Dn42CtlError,
    get_proposal,
    get_report,
    list_proposals,
    list_reports,
    submit_proposal,
    submit_report,
)

NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


def _register(db_path: Path) -> None:
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).add(NODE_A, "alpha")
    finally:
        db.close()


class TestSubmitProposal:
    def test_basic(self, db_path: Path) -> None:
        _register(db_path)
        p = submit_proposal(db_path=db_path, node_id=NODE_A, source="push", kind="peer_add", payload={"asn": 1})
        assert p.status == "pending"
        assert p.kind == "peer_add"

    def test_unknown_node(self, db_path: Path) -> None:
        # Database must exist so we just create it empty.
        Database.open(db_path).close()
        with pytest.raises(Dn42CtlError, match="不存在"):
            submit_proposal(db_path=db_path, node_id=NODE_A, source="push", kind="peer_add", payload={})

    def test_invalid_kind(self, db_path: Path) -> None:
        _register(db_path)
        with pytest.raises(Dn42CtlError, match="kind"):
            submit_proposal(db_path=db_path, node_id=NODE_A, source="push", kind="bogus", payload={})

    def test_invalid_source(self, db_path: Path) -> None:
        _register(db_path)
        with pytest.raises(Dn42CtlError, match="source"):
            submit_proposal(db_path=db_path, node_id=NODE_A, source="bogus", kind="peer_add", payload={})

    def test_invalid_payload(self, db_path: Path) -> None:
        _register(db_path)
        with pytest.raises(Dn42CtlError):
            submit_proposal(
                db_path=db_path,
                node_id=NODE_A,
                source="push",
                kind="peer_add",
                payload="x",  # type: ignore[arg-type]
            )

    def test_list_and_get(self, db_path: Path) -> None:
        _register(db_path)
        a = submit_proposal(db_path=db_path, node_id=NODE_A, source="push", kind="peer_add", payload={"n": 1})
        b = submit_proposal(db_path=db_path, node_id=NODE_A, source="scan", kind="peer_add", payload={"n": 2})
        listed = list_proposals(db_path=db_path, node_id=NODE_A)
        assert {p.id for p in listed} == {a.id, b.id}
        fetched = get_proposal(db_path=db_path, proposal_id=a.id)
        assert fetched.id == a.id


class TestSubmitReport:
    def test_basic(self, db_path: Path) -> None:
        _register(db_path)
        r = submit_report(db_path=db_path, node_id=NODE_A, kind="apply_result", payload={"ok": True})
        assert r.kind == "apply_result"
        assert r.imported_at is None

    def test_invalid_kind(self, db_path: Path) -> None:
        _register(db_path)
        with pytest.raises(Dn42CtlError, match="kind"):
            submit_report(db_path=db_path, node_id=NODE_A, kind="x", payload={})

    def test_unknown_node(self, db_path: Path) -> None:
        Database.open(db_path).close()
        with pytest.raises(Dn42CtlError, match="不存在"):
            submit_report(db_path=db_path, node_id=NODE_A, kind="apply_result", payload={})

    def test_list_filter(self, db_path: Path) -> None:
        _register(db_path)
        submit_report(db_path=db_path, node_id=NODE_A, kind="apply_result", payload={})
        submit_report(db_path=db_path, node_id=NODE_A, kind="scan_result", payload={})
        only_apply = list_reports(db_path=db_path, node_id=NODE_A, kind="apply_result")
        assert len(only_apply) == 1

    def test_get_missing(self, db_path: Path) -> None:
        _register(db_path)
        with pytest.raises(Dn42CtlError, match="不存在"):
            get_report(db_path=db_path, report_id=9999)
