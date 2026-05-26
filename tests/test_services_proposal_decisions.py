from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from argon2 import PasswordHasher
from conftest import VALID_ENDPOINT, VALID_PEER_IP, VALID_PEER_LLA, VALID_PUBKEY

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import (
    Dn42CtlError,
    accept_proposal,
    import_report,
    list_proposals,
    list_reports,
    reject_proposal,
    submit_proposal,
    submit_report,
)

NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _fast_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("dn42ctl.db_managed._password_hasher", cheap)
    yield


@pytest.fixture(autouse=True)
def _mock_wg(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub WireGuard generation so create_*_peer doesn't shell out."""
    from conftest import FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY

    with (
        patch(
            "dn42ctl.services.core.generate_wg_keypair",
            return_value=(FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY),
        ),
        patch(
            "dn42ctl.services.bgp.generate_random_lla",
            return_value="fe80::abcd:1234",
        ),
        patch(
            "dn42ctl.services.ibgp.generate_random_lla",
            return_value="fe80::abcd:5678",
        ),
    ):
        yield


def _register(db_path: Path) -> None:
    db = Database.open(db_path)
    try:
        ManagedNodeStore(db.connection).add(NODE_A, "alpha")
    finally:
        db.close()


def _bgp_add_payload(asn: int = 4242421234) -> dict:
    return {
        "peer_kind": "bgp",
        "peer": {
            "peer_asn": asn,
            "peer_public_key": VALID_PUBKEY,
            "endpoint": VALID_ENDPOINT,
            "peer_lla": VALID_PEER_LLA,
            "net_backend": "networkd",
        },
    }


def _ibgp_add_payload(name: str = "alpha") -> dict:
    return {
        "peer_kind": "ibgp",
        "peer": {
            "name": name,
            "peer_ip": VALID_PEER_IP,
            "has_wg": False,
        },
    }


class TestAcceptBgpAdd:
    def test_creates_bgp_peer(self, sample_config: AppConfig, db_path: Path) -> None:
        # The proposal's node_id is NODE_A, so the peer must land in NODE_A's table,
        # not in the central host's own self node (sample_config.node_id).
        _register(db_path)
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        result = accept_proposal(config=sample_config, db_path=db_path, proposal_id=p.id)
        assert result.status == "accepted"
        assert result.decided_at is not None
        db = Database.open(db_path)
        try:
            # Lands in NODE_A (the reporting node), not the central self.
            assert db.get_bgp_peer(NODE_A, 4242421234) is not None
            assert db.get_bgp_peer(sample_config.node_id, 4242421234) is None
        finally:
            db.close()


class TestAcceptIbgpAdd:
    def test_creates_ibgp_peer(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_ibgp_add_payload(),
        )
        result = accept_proposal(config=sample_config, db_path=db_path, proposal_id=p.id)
        assert result.status == "accepted"
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(NODE_A, "alpha") is not None
            assert db.get_ibgp_peer(sample_config.node_id, "alpha") is None
        finally:
            db.close()


class TestAcceptFailureKeepsPending:
    def test_duplicate_asn(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        # First accept: ok
        p1 = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        accept_proposal(config=sample_config, db_path=db_path, proposal_id=p1.id)
        # Second accept of same ASN: must fail and keep pending.
        p2 = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        with pytest.raises(Dn42CtlError):
            accept_proposal(config=sample_config, db_path=db_path, proposal_id=p2.id)
        p2_after = list_proposals(db_path=db_path, node_id=NODE_A, status="pending")
        assert any(x.id == p2.id for x in p2_after)


class TestAcceptDelete:
    def test_deletes(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        add = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        accept_proposal(config=sample_config, db_path=db_path, proposal_id=add.id)
        delete = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_delete",
            payload={"peer_kind": "bgp", "key": {"peer_asn": 4242421234}},
        )
        accept_proposal(config=sample_config, db_path=db_path, proposal_id=delete.id)
        db = Database.open(db_path)
        try:
            assert db.get_bgp_peer(NODE_A, 4242421234) is None
        finally:
            db.close()


class TestReject:
    def test_marks_rejected(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        result = reject_proposal(db_path=db_path, proposal_id=p.id, reason="not now")
        assert result.status == "rejected"
        assert result.message == "not now"

    def test_empty_reason(self, db_path: Path) -> None:
        _register(db_path)
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        with pytest.raises(Dn42CtlError, match="reason"):
            reject_proposal(db_path=db_path, proposal_id=p.id, reason="   ")


class TestAcceptAlreadyDecided:
    def test_cannot_reaccept(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        accept_proposal(config=sample_config, db_path=db_path, proposal_id=p.id)
        with pytest.raises(Dn42CtlError, match="accepted"):
            accept_proposal(config=sample_config, db_path=db_path, proposal_id=p.id)


class TestAutoAccept:
    def test_peer_add_auto_accept_writes(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        # Switch policy.
        from dn42ctl.services import set_policy

        set_policy(db_path=db_path, node_id=NODE_A, peer_add="auto_accept")
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
            config=sample_config,
        )
        assert p.status == "accepted"
        db = Database.open(db_path)
        try:
            assert db.get_bgp_peer(NODE_A, 4242421234) is not None
        finally:
            db.close()

    def test_auto_accept_failure_marks_rejected(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        from dn42ctl.services import set_policy

        set_policy(db_path=db_path, node_id=NODE_A, peer_add="auto_accept")
        # Submit twice; second fails validation (duplicate ASN).
        submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
            config=sample_config,
        )
        p2 = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
            config=sample_config,
        )
        assert p2.status == "rejected"
        assert p2.message and "auto_accept" in p2.message


class TestAcceptDoesNotRenderFiles:
    def test_no_bird_or_networkd_files_written(self, sample_config: AppConfig, db_path: Path) -> None:
        """accept_proposal must not write /etc/bird, /etc/systemd/network etc.

        The server runs sandboxed and those paths are out of bounds; the spoke
        renders them on next pull/apply.
        """
        peers_dir = Path(sample_config.bird_peers_dir)
        networkd_dir = Path(sample_config.networkd_dir)
        before_peers = set(peers_dir.iterdir())
        before_networkd = set(networkd_dir.iterdir())

        _register(db_path)
        p = submit_proposal(
            db_path=db_path,
            node_id=NODE_A,
            source="push",
            kind="peer_add",
            payload=_bgp_add_payload(),
        )
        accept_proposal(config=sample_config, db_path=db_path, proposal_id=p.id)

        assert set(peers_dir.iterdir()) == before_peers
        assert set(networkd_dir.iterdir()) == before_networkd


class TestImportReport:
    def test_imports_scan_result(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        r = submit_report(
            db_path=db_path,
            node_id=NODE_A,
            kind="scan_result",
            payload={
                "bgp_peers": [_bgp_add_payload()["peer"]],
                "ibgp_peers": [_ibgp_add_payload()["peer"]],
            },
        )
        counts = import_report(config=sample_config, db_path=db_path, report_id=r.id)
        assert counts == {"bgp_created": 1, "bgp_skipped": 0, "ibgp_created": 1, "ibgp_skipped": 0}
        listed = list_reports(db_path=db_path, node_id=NODE_A)
        assert listed[0].imported_at is not None
        # The created peer must belong to NODE_A (the reporting node),
        # not the central host's own self node.
        db = Database.open(db_path)
        try:
            assert db.get_bgp_peer(NODE_A, 4242421234) is not None
            assert db.get_bgp_peer(sample_config.node_id, 4242421234) is None
            assert db.get_ibgp_peer(NODE_A, "alpha") is not None
            assert db.get_ibgp_peer(sample_config.node_id, "alpha") is None
        finally:
            db.close()

    def test_reimport_skips_existing(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        r1 = submit_report(
            db_path=db_path,
            node_id=NODE_A,
            kind="scan_result",
            payload={"bgp_peers": [_bgp_add_payload()["peer"]], "ibgp_peers": []},
        )
        import_report(config=sample_config, db_path=db_path, report_id=r1.id)
        # Second submission, second import -> peer exists, skipped.
        r2 = submit_report(
            db_path=db_path,
            node_id=NODE_A,
            kind="scan_result",
            payload={"bgp_peers": [_bgp_add_payload()["peer"]], "ibgp_peers": []},
        )
        counts = import_report(config=sample_config, db_path=db_path, report_id=r2.id)
        assert counts["bgp_skipped"] == 1
        assert counts["bgp_created"] == 0

    def test_reject_non_scan_kind(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        r = submit_report(db_path=db_path, node_id=NODE_A, kind="apply_result", payload={})
        with pytest.raises(Dn42CtlError, match="scan_result"):
            import_report(config=sample_config, db_path=db_path, report_id=r.id)

    def test_reject_double_import(self, sample_config: AppConfig, db_path: Path) -> None:
        _register(db_path)
        r = submit_report(
            db_path=db_path,
            node_id=NODE_A,
            kind="scan_result",
            payload={"bgp_peers": [], "ibgp_peers": []},
        )
        import_report(config=sample_config, db_path=db_path, report_id=r.id)
        with pytest.raises(Dn42CtlError, match="已被导入"):
            import_report(config=sample_config, db_path=db_path, report_id=r.id)
