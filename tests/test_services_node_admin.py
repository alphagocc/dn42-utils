from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from dn42ctl.services import (
    Dn42CtlError,
    add_node,
    get_node,
    list_nodes,
    remove_node,
    rotate_token,
    set_policy,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_BAD = "not-a-uuid"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


class TestAddNode:
    def test_basic(self, db_path: Path) -> None:
        node = add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        assert node.node_id == NODE_A
        assert node.name == "alpha"

    def test_invalid_uuid(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="UUID"):
            add_node(db_path=db_path, node_id=NODE_BAD, name="alpha")

    def test_empty_name(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="name"):
            add_node(db_path=db_path, node_id=NODE_A, name="   ")

    def test_duplicate(self, db_path: Path) -> None:
        add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        with pytest.raises(Dn42CtlError):
            add_node(db_path=db_path, node_id=NODE_A, name="alpha")


class TestListGet:
    def test_list_empty(self, db_path: Path) -> None:
        assert list_nodes(db_path=db_path) == []

    def test_get_missing(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            get_node(db_path=db_path, node_id=NODE_A)


class TestRemove:
    def test_remove(self, db_path: Path) -> None:
        add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        removed = remove_node(db_path=db_path, node_id=NODE_A)
        assert removed.node_id == NODE_A
        with pytest.raises(Dn42CtlError):
            get_node(db_path=db_path, node_id=NODE_A)

    def test_remove_missing(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            remove_node(db_path=db_path, node_id=NODE_A)


class TestRotateToken:
    def test_returns_plaintext(self, db_path: Path) -> None:
        add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        rotated = rotate_token(db_path=db_path, node_id=NODE_A)
        assert rotated.node_id == NODE_A
        assert len(rotated.plaintext) >= 32

    def test_two_rotates_different(self, db_path: Path) -> None:
        add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        a = rotate_token(db_path=db_path, node_id=NODE_A)
        b = rotate_token(db_path=db_path, node_id=NODE_A)
        assert a.plaintext != b.plaintext


class TestSetPolicy:
    def test_partial_update(self, db_path: Path) -> None:
        add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        updated = set_policy(db_path=db_path, node_id=NODE_A, peer_add="auto_accept")
        assert updated.write_policy["peer_add"] == "auto_accept"
        assert updated.write_policy["peer_modify"] == "review"

    def test_reject_invalid_peer_modify(self, db_path: Path) -> None:
        add_node(db_path=db_path, node_id=NODE_A, name="alpha")
        with pytest.raises(Dn42CtlError, match="peer_modify"):
            set_policy(db_path=db_path, node_id=NODE_A, peer_modify="auto_accept")

    def test_missing_node(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="不存在"):
            set_policy(db_path=db_path, node_id=NODE_A, peer_add="review")
