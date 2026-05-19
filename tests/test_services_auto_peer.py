"""Tests for dn42ctl.services.auto_peer — challenge/session flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
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
    # bootstrap self node
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
