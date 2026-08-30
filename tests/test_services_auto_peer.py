"""Tests for dn42ctl.services.auto_peer — peer session issuing and proposal submission."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import auto_peer
from dn42ctl.services.auto_peer import (
    AutoPeerError,
    AutoPeerExpiredError,
    list_peer_targets,
    open_session,
    reset_state,
    submit_peer,
)
from dn42ctl.services.kioubit_auth import KioubitIdentity

WG_PUBKEY = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="
NODE_ID = "test-node"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_state()
    yield
    reset_state()


def _identity(digest: str = "digest-1", asn: int = 4242421234) -> KioubitIdentity:
    return KioubitIdentity(
        asn=asn,
        mntner="TEST-MNT",
        authtype="logincode",
        issued_at=1668266926.0,
        digest=digest,
    )


@pytest.fixture
def db_with_self(db_path: Path) -> Path:
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        store.upsert_self(NODE_ID, name="self")
        store.apply_address_update(NODE_ID, auto_peer=True)
    finally:
        db.close()
    return db_path


def _close_auto_peer(db_path: Path, node_id: str = NODE_ID) -> None:
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).apply_address_update(node_id, auto_peer=False)
    finally:
        db.close()


def test_list_peer_targets_only_returns_open_nodes(db_with_self: Path) -> None:
    assert [t.node_id for t in list_peer_targets(db_path=db_with_self)] == [NODE_ID]

    _close_auto_peer(db_with_self)
    assert list_peer_targets(db_path=db_with_self) == []


def test_open_session_returns_verified_identity() -> None:
    session = open_session(_identity())
    assert session.verified_asn == 4242421234
    assert session.verified_mntner == "TEST-MNT"
    assert session.peer_session_token


def test_same_auth_response_cannot_be_replayed() -> None:
    open_session(_identity())
    with pytest.raises(AutoPeerExpiredError, match="已被使用"):
        open_session(_identity())


def test_distinct_auth_responses_each_get_a_session() -> None:
    first = open_session(_identity("digest-1"))
    second = open_session(_identity("digest-2"))
    assert first.peer_session_token != second.peer_session_token


def test_submit_peer_creates_proposal(sample_config: AppConfig, db_with_self: Path, mock_wg_keypair) -> None:
    session = open_session(_identity())

    result = submit_peer(
        config=sample_config,
        db_path=db_with_self,
        session_token=session.peer_session_token,
        node_id=NODE_ID,
        wg_public_key=WG_PUBKEY,
        endpoint="example.com:51820",
        peer_lla="fe80::1",
    )
    assert result.proposal.kind == "peer_add"
    assert result.proposal.status == "pending"
    assert result.proposal.node_id == "test-node"


def test_session_burns_after_submit(sample_config: AppConfig, db_with_self: Path, mock_wg_keypair) -> None:
    session = open_session(_identity())

    submit_peer(
        config=sample_config,
        db_path=db_with_self,
        session_token=session.peer_session_token,
        node_id=NODE_ID,
        wg_public_key=WG_PUBKEY,
        endpoint="",
        peer_lla="fe80::1",
    )
    with pytest.raises(AutoPeerExpiredError):
        submit_peer(
            config=sample_config,
            db_path=db_with_self,
            session_token=session.peer_session_token,
            node_id=NODE_ID,
            wg_public_key=WG_PUBKEY,
            endpoint="",
            peer_lla="fe80::2",
        )


def test_unknown_session_token_is_rejected(sample_config: AppConfig, db_with_self: Path) -> None:
    with pytest.raises(AutoPeerExpiredError):
        submit_peer(
            config=sample_config,
            db_path=db_with_self,
            session_token="not-a-token",
            node_id=NODE_ID,
            wg_public_key=WG_PUBKEY,
            endpoint="",
            peer_lla="fe80::1",
        )


def test_submit_to_a_closed_node_is_rejected(sample_config: AppConfig, db_with_self: Path) -> None:
    """开关关掉之后,停留在旧页面上的请求也提交不进来。"""
    session = open_session(_identity())
    _close_auto_peer(db_with_self)

    with pytest.raises(AutoPeerError, match="不接受 auto-peer"):
        submit_peer(
            config=sample_config,
            db_path=db_with_self,
            session_token=session.peer_session_token,
            node_id=NODE_ID,
            wg_public_key=WG_PUBKEY,
            endpoint="",
            peer_lla="fe80::1",
        )


def test_submit_to_an_unknown_node_is_rejected(sample_config: AppConfig, db_with_self: Path) -> None:
    session = open_session(_identity())
    with pytest.raises(AutoPeerError, match="不接受 auto-peer"):
        submit_peer(
            config=sample_config,
            db_path=db_with_self,
            session_token=session.peer_session_token,
            node_id="no-such-node",
            wg_public_key=WG_PUBKEY,
            endpoint="",
            peer_lla="fe80::1",
        )


class TestConcurrentSingleUse:
    """ "一次性"必须在并发下也成立。

    提案写库发生在锁外,并发请求会同时读到同一个 session、各自建一条提案。in-flight
    标记让第一个请求认领,其余立刻被拒。
    """

    def test_one_session_yields_one_proposal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sample_config: AppConfig,
        db_with_self: Path,
        mock_wg_keypair,
    ) -> None:
        real_submit_proposal = auto_peer.submit_proposal

        def slow_submit(**kwargs: object):
            # 窗口远大于线程启动开销,后来者必然撞上 in-flight。
            time.sleep(0.2)
            return real_submit_proposal(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(auto_peer, "submit_proposal", slow_submit)
        session = open_session(_identity())

        results: list[object] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                out: object = submit_peer(
                    config=sample_config,
                    db_path=db_with_self,
                    session_token=session.peer_session_token,
                    node_id=NODE_ID,
                    wg_public_key=WG_PUBKEY,
                    endpoint="",
                    peer_lla="fe80::1",
                )
            except AutoPeerError as exc:
                out = exc
            with lock:
                results.append(out)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        accepted = [r for r in results if not isinstance(r, Exception)]
        assert len(accepted) == 1
        assert auto_peer._sessions == {}
        assert all("正在处理中" in str(r) for r in results if isinstance(r, Exception))

    def test_failed_submit_allows_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sample_config: AppConfig,
        db_with_self: Path,
        mock_wg_keypair,
    ) -> None:
        """失败保留以容许重试。in-flight 标记不能把 session 卡死到 TTL。"""

        def boom(**_kwargs: object):
            raise AutoPeerError("提案写入失败")

        monkeypatch.setattr(auto_peer, "submit_proposal", boom)
        session = open_session(_identity())

        for _ in range(2):
            with pytest.raises(AutoPeerError, match="提案写入失败"):
                submit_peer(
                    config=sample_config,
                    db_path=db_with_self,
                    session_token=session.peer_session_token,
                    node_id=NODE_ID,
                    wg_public_key=WG_PUBKEY,
                    endpoint="",
                    peer_lla="fe80::1",
                )

        assert auto_peer._sessions[session.peer_session_token].in_flight is False
