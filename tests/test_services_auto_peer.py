"""Tests for dn42ctl.services.auto_peer — challenge/session flow."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import auto_peer
from dn42ctl.services.auto_peer import (
    AutoPeerError,
    AutoPeerExpiredError,
    reset_state,
    start_challenge,
    start_lookup,
    submit_peer,
    verify_challenge,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_state()
    yield
    reset_state()


def _config_with_registry(sample_config: AppConfig, registry: Path) -> AppConfig:
    return AppConfig(
        **{
            **{f.name: getattr(sample_config, f.name) for f in sample_config.__dataclass_fields__.values()},
            "dn42_registry_path": str(registry),
        }
    )


def test_start_lookup(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    result = start_lookup(config=cfg, asn=4242421234)
    assert result.asn == 4242421234
    assert len(result.mntners) == 2
    assert result.mntners[0].name == "TEST-MNT"
    assert len(result.mntners[0].auth_options) == 2  # ssh + pgp (ed25519-pw filtered)


def test_start_lookup_no_registry(sample_config: AppConfig) -> None:
    with pytest.raises(AutoPeerError, match="未启用"):
        start_lookup(config=sample_config, asn=4242421234)


def test_start_challenge(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    challenge = start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=0)
    assert challenge.scheme == "ssh"
    assert len(challenge.nonce_hex) == 64
    assert challenge.namespace == "dn42ctl-autopeer"


def test_start_challenge_wrong_mntner(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    with pytest.raises(AutoPeerError, match="mnt-by"):
        start_challenge(config=cfg, asn=4242421234, mntner="WRONG-MNT", auth_index=0)


def test_start_challenge_bad_index(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    with pytest.raises(AutoPeerError, match="越界"):
        start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=99)


def test_verify_challenge_success(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    challenge = start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=0)

    with patch("dn42ctl.services.auto_peer.verify_ssh", return_value=True):
        result = verify_challenge(config=cfg, challenge_id=challenge.challenge_id, signature="fake-sig")
    assert result.verified_asn == 4242421234
    assert result.verified_mntner == "TEST-MNT"
    assert result.peer_session_token


def test_verify_challenge_fail(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    challenge = start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=0)

    with (
        patch("dn42ctl.services.auto_peer.verify_ssh", return_value=False),
        pytest.raises(AutoPeerError, match="校验失败"),
    ):
        verify_challenge(config=cfg, challenge_id=challenge.challenge_id, signature="bad")


def test_verify_challenge_expired(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    with pytest.raises(AutoPeerExpiredError):
        verify_challenge(config=cfg, challenge_id="nonexistent", signature="x")


def test_challenge_burns_after_success(sample_config: AppConfig, dn42_registry: Path) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    challenge = start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=0)

    with patch("dn42ctl.services.auto_peer.verify_ssh", return_value=True):
        verify_challenge(config=cfg, challenge_id=challenge.challenge_id, signature="ok")

    with pytest.raises(AutoPeerExpiredError):
        verify_challenge(config=cfg, challenge_id=challenge.challenge_id, signature="again")


def test_submit_peer_creates_proposal(
    sample_config: AppConfig,
    dn42_registry: Path,
    db_path: Path,
    mock_wg_keypair,
) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).upsert_self("test-node", name="self")
    finally:
        db.close()

    challenge = start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=0)
    with patch("dn42ctl.services.auto_peer.verify_ssh", return_value=True):
        session = verify_challenge(config=cfg, challenge_id=challenge.challenge_id, signature="ok")

    result = submit_peer(
        config=cfg,
        db_path=db_path,
        session_token=session.peer_session_token,
        wg_public_key="YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=",
        endpoint="example.com:51820",
        peer_lla="fe80::1",
    )
    assert result.proposal.kind == "peer_add"
    assert result.proposal.status == "pending"
    assert result.proposal.node_id == "test-node"


def test_session_burns_after_submit(
    sample_config: AppConfig,
    dn42_registry: Path,
    db_path: Path,
    mock_wg_keypair,
) -> None:
    cfg = _config_with_registry(sample_config, dn42_registry)
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).upsert_self("test-node", name="self")
    finally:
        db.close()

    challenge = start_challenge(config=cfg, asn=4242421234, mntner="TEST-MNT", auth_index=0)
    with patch("dn42ctl.services.auto_peer.verify_ssh", return_value=True):
        session = verify_challenge(config=cfg, challenge_id=challenge.challenge_id, signature="ok")

    submit_peer(
        config=cfg,
        db_path=db_path,
        session_token=session.peer_session_token,
        wg_public_key="YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=",
        endpoint="",
        peer_lla="fe80::1",
    )
    with pytest.raises(AutoPeerExpiredError):
        submit_peer(
            config=cfg,
            db_path=db_path,
            session_token=session.peer_session_token,
            wg_public_key="YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=",
            endpoint="",
            peer_lla="fe80::2",
        )


class TestConcurrentSingleUse:
    """ "一次性"必须在并发下也成立。

    验证要跑子进程,不能持锁,所以锁外做验证会退化成 check-then-act:并发请求都读到
    同一个 challenge、都验签成功、都换出各自的 session。见 docs/architecture/auto_peer.md。
    """

    def _seed_challenge(self, cid: str) -> None:
        with auto_peer._lock:
            auto_peer._challenges[cid] = auto_peer._Challenge(
                id=cid,
                nonce=b"\x00" * 32,
                namespace=auto_peer.CHALLENGE_NAMESPACE,
                asn=4242421234,
                mntner="MNT",
                scheme="ssh",
                auth_raw="ssh-ed25519 AAAA",
                fingerprint=None,
                expires_at=auto_peer._now() + 600,
            )

    def test_one_challenge_yields_one_session(
        self, monkeypatch: pytest.MonkeyPatch, sample_config: AppConfig, dn42_registry: Path
    ) -> None:
        cfg = _config_with_registry(sample_config, dn42_registry)

        def slow_verify(**_kwargs: object) -> bool:
            # 子进程验证的真实耗时;窗口远大于线程启动开销,后来者必然撞上 in-flight。
            time.sleep(0.2)
            return True

        monkeypatch.setattr(auto_peer, "verify_ssh", slow_verify)
        self._seed_challenge("CID")

        results: list[object] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                out: object = verify_challenge(config=cfg, challenge_id="CID", signature="sig")
            except AutoPeerError as exc:
                out = exc
            with lock:
                results.append(out)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        issued = [r for r in results if not isinstance(r, Exception)]
        assert len(issued) == 1
        assert len(auto_peer._sessions) == 1
        assert all("正在校验中" in str(r) for r in results if isinstance(r, Exception))

    def test_failed_verification_allows_retry(
        self, monkeypatch: pytest.MonkeyPatch, sample_config: AppConfig, dn42_registry: Path
    ) -> None:
        """失败保留以容许重试 —— in-flight 标记不能把 challenge 卡死到 TTL。"""
        cfg = _config_with_registry(sample_config, dn42_registry)
        monkeypatch.setattr(auto_peer, "verify_ssh", lambda **_kw: False)
        self._seed_challenge("CID")

        with pytest.raises(AutoPeerError, match="签名校验失败"):
            verify_challenge(config=cfg, challenge_id="CID", signature="sig")
        with pytest.raises(AutoPeerError, match="签名校验失败"):
            verify_challenge(config=cfg, challenge_id="CID", signature="sig")

        assert auto_peer._challenges["CID"].in_flight is False
