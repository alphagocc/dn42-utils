from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.scan import (
    _parse_babel_conf_interface_params,
    _parse_bird_bgp_peer_conf,
    _parse_networkd_netdev,
    _parse_networkd_network,
    _parse_nmconnection,
    discover_bird_paths,
    scan_local_configs,
)


class TestParseNetworkdNetdev:
    def test_basic(self) -> None:
        text = """\
[NetDev]
Name=dn42_1234
Kind=wireguard

[WireGuard]
PrivateKey=TESTPRIVKEY
ListenPort=51820
RouteTable=off

[WireGuardPeer]
PublicKey=TESTPUBKEY
Endpoint=example.com:51820
AllowedIPs=fe80::/64,fd00::/8
"""
        result = _parse_networkd_netdev(text)
        assert result["private_key"] == "TESTPRIVKEY"
        assert result["listen_port"] == 51820
        assert result["peer_public_key"] == "TESTPUBKEY"
        assert result["endpoint"] == "example.com:51820"
        assert result["allowed_ips"] == ["fe80::/64", "fd00::/8"]

    def test_no_listen_port(self) -> None:
        text = """\
[WireGuard]
PrivateKey=KEY
RouteTable=off

[WireGuardPeer]
PublicKey=PUB
AllowedIPs=::/0
"""
        result = _parse_networkd_netdev(text)
        assert "listen_port" not in result

    def test_comments_skipped(self) -> None:
        text = """\
# This is a comment
[WireGuard]
PrivateKey=KEY  # inline comment
"""
        result = _parse_networkd_netdev(text)
        assert result["private_key"] == "KEY"


class TestParseNetworkdNetwork:
    def test_basic(self) -> None:
        text = """\
[Match]
Name=dn42_1234

[Address]
Address=fe80::abcd:1234/64
Peer=fe80::1
"""
        result = _parse_networkd_network(text)
        assert result["local_lla"] == "fe80::abcd:1234/64"
        assert result["peer_lla"] == "fe80::1"


class TestParseNmconnection:
    def test_basic(self) -> None:
        text = """\
[connection]
id=dn42_1234
type=wireguard

[wireguard]
private-key=PRIVKEY
listen-port=51820
peers=PUBKEY endpoint=example.com:51820 allowed-ips=fe80::/64;fd00::/8;

[ipv6]
method=manual
address1=fe80::abcd:1234/64
"""
        result = _parse_nmconnection(text)
        assert result["private_key"] == "PRIVKEY"
        assert result["listen_port"] == 51820
        assert result["peer_public_key"] == "PUBKEY"
        assert result["endpoint"] == "example.com:51820"
        assert result["allowed_ips"] == ["fe80::/64", "fd00::/8"]
        assert result["local_lla"] == "fe80::abcd:1234/64"


class TestParseBirdBgpPeerConf:
    def test_basic(self) -> None:
        text = """\
protocol bgp dn42_1234 from dnpeers {
    neighbor fe80::1%dn42_1234 as 4242421234;
}
"""
        asn, peer_lla = _parse_bird_bgp_peer_conf(text, "dn42_1234")
        assert asn == 4242421234
        assert peer_lla == "fe80::1"

    def test_no_match(self) -> None:
        asn, peer_lla = _parse_bird_bgp_peer_conf("garbage", "dn42_1234")
        assert asn is None
        assert peer_lla is None


class TestParseBabelConfInterfaceParams:
    def test_multiple_interfaces(self) -> None:
        text = """\
protocol babel intra_babel {
    interface "wg_node1" {
        type tunnel;
        rxcost 120;
    };
    interface "wg_node2" {
        type wired;
        rxcost 256;
    };
};
"""
        result = _parse_babel_conf_interface_params(text)
        assert "wg_node1" in result
        assert result["wg_node1"].rxcost == 120
        assert result["wg_node1"].babel_type == "tunnel"
        assert "wg_node2" in result
        assert result["wg_node2"].rxcost == 256
        assert result["wg_node2"].babel_type == "wired"

    def test_empty(self) -> None:
        result = _parse_babel_conf_interface_params("")
        assert result == {}


class TestDiscoverBirdPaths:
    def test_with_includes(self, tmp_path: Path) -> None:
        bird_conf = tmp_path / "bird.conf"
        bird_conf.write_text(
            'include "/etc/bird/peers/*";\ninclude "/etc/bird/babel.conf";\ninclude "/etc/bird/roa_dn42_v6.conf";\n'
        )
        result = discover_bird_paths(candidate_bird_conf_paths=[bird_conf])
        assert result.bird_conf_path == bird_conf
        assert result.bird_peers_dir == Path("/etc/bird/peers")
        assert result.bird_babel_conf_path == Path("/etc/bird/babel.conf")
        assert result.bird_roa_v6_conf_path == Path("/etc/bird/roa_dn42_v6.conf")

    def test_no_candidates(self) -> None:
        result = discover_bird_paths(candidate_bird_conf_paths=[Path("/nonexistent/bird.conf")])
        assert result.bird_conf_path is None
        assert result.bird_peers_dir is None


class TestScanLocalConfigs:
    def test_wg_not_found_raises(self, sample_config, db_path: Path) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(Dn42CtlError, match="wg"):
            scan_local_configs(config=sample_config, db_path=db_path)

    def test_scan_networkd_bgp(self, sample_config, db_path: Path) -> None:
        networkd_dir = Path(sample_config.networkd_dir)
        peers_dir = Path(sample_config.bird_peers_dir)

        netdev = networkd_dir / "dn42_1234.netdev"
        netdev.write_text(
            "[NetDev]\nName=dn42_1234\nKind=wireguard\n\n"
            "[WireGuard]\nPrivateKey=TESTPRIVKEY\nListenPort=51820\n\n"
            "[WireGuardPeer]\nPublicKey=TESTPUBKEY\nEndpoint=example.com:51820\n"
            "AllowedIPs=fe80::/64,fd00::/8\n"
        )
        network = networkd_dir / "dn42_1234.network"
        network.write_text("[Match]\nName=dn42_1234\n\n[Address]\nAddress=fe80::abcd:1234/64\nPeer=fe80::1\n")
        bird_peer = peers_dir / "dn42_1234.conf"
        bird_peer.write_text(
            "protocol bgp dn42_1234 from dnpeers {\n    neighbor fe80::1%dn42_1234 as 4242421234;\n}\n"
        )

        with (
            patch("shutil.which", return_value="/usr/bin/wg"),
            patch(
                "dn42ctl.services.scan.pubkey_from_private",
                return_value="DERIVEDPUBKEY",
            ),
        ):
            result = scan_local_configs(config=sample_config, db_path=db_path)

        assert len(result.inserted) == 1
        assert result.inserted[0].kind == "bgp"
        assert result.inserted[0].key == "AS4242421234"
        assert result.inserted[0].net_backend == "networkd"

    def test_scan_conflict(self, sample_config, db_path: Path) -> None:
        networkd_dir = Path(sample_config.networkd_dir)
        peers_dir = Path(sample_config.bird_peers_dir)

        netdev = networkd_dir / "dn42_1234.netdev"
        netdev.write_text(
            "[WireGuard]\nPrivateKey=KEY\nListenPort=51820\n[WireGuardPeer]\nPublicKey=PUB\nAllowedIPs=fe80::/64\n"
        )
        network = networkd_dir / "dn42_1234.network"
        network.write_text("[Address]\nAddress=fe80::1/64\nPeer=fe80::2\n")
        bird_peer = peers_dir / "dn42_1234.conf"
        bird_peer.write_text("neighbor fe80::2%dn42_1234 as 4242421234;")

        with (
            patch("shutil.which", return_value="/usr/bin/wg"),
            patch("dn42ctl.services.scan.pubkey_from_private", return_value="PUB"),
        ):
            scan_local_configs(config=sample_config, db_path=db_path)
            result = scan_local_configs(config=sample_config, db_path=db_path)

        assert len(result.conflicts) == 1
