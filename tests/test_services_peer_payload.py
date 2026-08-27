"""Node-supplied payloads must be validated before they reach the service layer.

A malformed peer is not a local failure: `bird.conf` does `include "<peers_dir>/*";`,
so one unparseable peer file takes down the whole BIRD config on that node.
"""

from __future__ import annotations

from typing import Any

import pytest

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.peer_payload import parse_bgp_key, parse_bgp_peer, parse_ibgp_key, parse_ibgp_peer

VALID_PUBKEY = "A" * 43 + "="


def _bgp(**overrides: Any) -> dict[str, Any]:
    peer = {
        "peer_asn": 4242420000,
        "peer_public_key": VALID_PUBKEY,
        "endpoint": "example.com:51820",
        "peer_lla": "fe80::1",
    }
    peer.update(overrides)
    return peer


def _ibgp(**overrides: Any) -> dict[str, Any]:
    peer = {
        "name": "alpha",
        "peer_ip": "fd42::1",
        "has_wg": True,
        "peer_public_key": VALID_PUBKEY,
        "peer_lla": "fe80::2",
        "babel_rxcost": 20,
        "babel_type": "tunnel",
    }
    peer.update(overrides)
    return peer


class TestBgpPayload:
    def test_valid_payload_round_trips(self) -> None:
        parsed = parse_bgp_peer(_bgp())
        assert parsed.peer_asn == 4242420000
        assert parsed.net_backend == "networkd"
        assert parsed.listen_port is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("peer_asn", -1),
            ("peer_asn", 0),
            ("peer_asn", "AS4242420000"),
            ("peer_asn", 4242420000.9),
            ("peer_asn", True),
            ("peer_public_key", ""),
            ("peer_public_key", "not-base64!!"),
            ("peer_public_key", None),
            ("peer_lla", "not-ip"),
            ("peer_lla", "192.0.2.1"),
            ("peer_lla", ""),
            ("endpoint", "no-port"),
            ("endpoint", "example.com:99999"),
            ("net_backend", "bogus"),
            ("listen_port", -1),
            ("listen_port", 70000),
            ("listen_port", "51820"),
        ],
    )
    def test_rejects_bad_field(self, field: str, value: Any) -> None:
        with pytest.raises(Dn42CtlError):
            parse_bgp_peer(_bgp(**{field: value}))

    @pytest.mark.parametrize("field", ["peer_asn", "peer_public_key", "peer_lla"])
    def test_rejects_missing_field(self, field: str) -> None:
        peer = _bgp()
        del peer[field]
        with pytest.raises(Dn42CtlError, match="缺少必填字段"):
            parse_bgp_peer(peer)

    def test_huge_asn_is_rejected_not_crashed(self) -> None:
        """sqlite 存不下 2^63,原先这里抛 OverflowError 变成 HTTP 500。"""
        with pytest.raises(Dn42CtlError):
            parse_bgp_peer(_bgp(peer_asn=10**30))


class TestIbgpPayload:
    def test_valid_payload_round_trips(self) -> None:
        parsed = parse_ibgp_peer(_ibgp())
        assert parsed.name == "alpha"
        assert parsed.has_wg is True
        assert parsed.babel_rxcost == 20

    def test_no_wg_peer_drops_tunnel_fields(self) -> None:
        """has_wg=false 的 peer 没有隧道,payload 里那两列本来就是空串。"""
        parsed = parse_ibgp_peer(_ibgp(has_wg=False, peer_public_key="", peer_lla=""))
        assert parsed.peer_public_key is None
        assert parsed.peer_lla is None

    def test_no_wg_peer_still_validates_peer_ip(self) -> None:
        with pytest.raises(Dn42CtlError):
            parse_ibgp_peer(_ibgp(has_wg=False, peer_public_key="", peer_lla="", peer_ip="nope"))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", ""),
            ("name", 42),
            ("peer_ip", "not-ip"),
            ("has_wg", "true"),
            ("has_wg", 1),
            ("peer_public_key", "short"),
            ("peer_lla", "not-ip"),
            ("babel_rxcost", -1),
            ("babel_rxcost", True),
            ("babel_type", "carrier-pigeon"),
        ],
    )
    def test_rejects_bad_field(self, field: str, value: Any) -> None:
        with pytest.raises(Dn42CtlError):
            parse_ibgp_peer(_ibgp(**{field: value}))

    @pytest.mark.parametrize("field", ["name", "peer_ip", "has_wg", "babel_rxcost", "babel_type"])
    def test_rejects_missing_field(self, field: str) -> None:
        peer = _ibgp()
        del peer[field]
        with pytest.raises(Dn42CtlError, match="缺少必填字段|不能为空"):
            parse_ibgp_peer(peer)

    def test_wg_peer_requires_tunnel_fields(self) -> None:
        peer = _ibgp()
        del peer["peer_public_key"]
        with pytest.raises(Dn42CtlError, match="缺少必填字段"):
            parse_ibgp_peer(peer)


class TestDeleteKeys:
    def test_bgp_key(self) -> None:
        assert parse_bgp_key({"peer_asn": 4242420000}) == 4242420000

    def test_bgp_key_rejects_garbage(self) -> None:
        with pytest.raises(Dn42CtlError):
            parse_bgp_key({"peer_asn": -1})

    def test_ibgp_key(self) -> None:
        assert parse_ibgp_key({"name": "alpha"}) == "alpha"

    def test_ibgp_key_rejects_blank(self) -> None:
        with pytest.raises(Dn42CtlError):
            parse_ibgp_key({"name": "   "})
