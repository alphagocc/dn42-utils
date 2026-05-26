from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.render import (
    nm_uuid_for,
    render_babel_conf,
    render_bird_bgp_peer_conf,
    render_bird_ibgp_peer_conf,
    render_bird_main_conf,
    render_networkd_netdev,
    render_networkd_network,
    render_nmconnection_wireguard,
    render_systemd_roa_service,
    render_systemd_roa_timer,
)


class TestRenderBirdBgpPeerConf:
    def test_basic(self) -> None:
        result = render_bird_bgp_peer_conf(ifname="dn42_1234", peer_lla="fe80::1", peer_asn=4242421234)
        assert "protocol bgp dn42_1234" in result
        assert "fe80::1" in result
        assert "4242421234" in result

    def test_empty_peer_lla_raises(self) -> None:
        with pytest.raises(ValueError, match="peer_lla must not be empty"):
            render_bird_bgp_peer_conf(ifname="dn42_1234", peer_lla="", peer_asn=4242421234)


class TestRenderBirdIbgpPeerConf:
    def test_basic(self) -> None:
        result = render_bird_ibgp_peer_conf(name="mynode", ifname="wg_mynode", peer_ip="fd42:4242:5678::1")
        assert "ibgp_mynode" in result
        assert "fd42:4242:5678::1" in result
        assert "OWNAS" in result

    def test_empty_peer_ip_raises(self) -> None:
        with pytest.raises(ValueError, match="peer_ip must not be empty"):
            render_bird_ibgp_peer_conf(name="mynode", ifname="wg_mynode", peer_ip="")


class TestRenderBabelConf:
    def test_with_interfaces(self) -> None:
        interfaces = [
            ("wg_node1", 120, "tunnel"),
            ("wg_node2", 256, "wired"),
        ]
        result = render_babel_conf(interfaces=interfaces)
        assert 'interface "wg_node1"' in result
        assert "rxcost 120" in result
        assert "type tunnel" in result
        assert 'interface "wg_node2"' in result
        assert "rxcost 256" in result
        assert "type wired" in result

    def test_empty_interfaces(self) -> None:
        result = render_babel_conf(interfaces=[])
        assert "protocol babel" in result
        assert "rxcost" not in result

    def test_contains_direct_protocol(self) -> None:
        result = render_babel_conf(interfaces=[])
        assert "protocol direct" in result
        assert 'interface "dn42-dummy"' in result


class TestRenderBirdMainConf:
    def test_basic(self) -> None:
        result = render_bird_main_conf(
            own_asn=4242421234,
            router_id="172.23.0.1",
            own_ipv6="fd42:4242:1234::1",
            ownnet_v6="fd42:4242:1234::/48",
            ownnetset_v6="[fd42:4242:1234::/48+]",
            bird_babel_conf_path=Path("/etc/bird/babel.conf"),
            bird_peers_dir=Path("/etc/bird/peers"),
            bird_roa_v6_conf_path=Path("/etc/bird/roa_dn42_v6.conf"),
        )
        assert "4242421234" in result
        assert "172.23.0.1" in result
        assert "fd42:4242:1234::1" in result
        assert "fd42:4242:1234::/48" in result
        assert "/etc/bird/babel.conf" in result
        assert "/etc/bird/peers/*" in result
        assert "/etc/bird/roa_dn42_v6.conf" in result


class TestRenderNetworkdNetdev:
    def test_basic(self) -> None:
        result = render_networkd_netdev(
            ifname="dn42_1234",
            private_key="PRIVKEY",
            listen_port=51820,
            peer_public_key="PUBKEY",
            endpoint="example.com:51820",
            allowed_ips=["fe80::/64", "fd00::/8"],
        )
        assert "[NetDev]" in result
        assert "Name=dn42_1234" in result
        assert "PrivateKey=PRIVKEY" in result
        assert "ListenPort=51820" in result
        assert "PublicKey=PUBKEY" in result
        assert "Endpoint=example.com:51820" in result
        assert "AllowedIPs=fe80::/64" in result
        assert "AllowedIPs=fd00::/8" in result
        assert "RouteTable=off" in result

    def test_zero_listen_port_omitted(self) -> None:
        result = render_networkd_netdev(
            ifname="dn42_1234",
            private_key="PRIVKEY",
            listen_port=0,
            peer_public_key="PUBKEY",
            endpoint="",
            allowed_ips=["fe80::/64"],
        )
        assert "ListenPort" not in result

    def test_empty_endpoint_omitted(self) -> None:
        result = render_networkd_netdev(
            ifname="dn42_1234",
            private_key="PRIVKEY",
            listen_port=51820,
            peer_public_key="PUBKEY",
            endpoint="",
            allowed_ips=["fe80::/64"],
        )
        assert "Endpoint" not in result


class TestRenderNetworkdNetwork:
    def test_basic(self) -> None:
        result = render_networkd_network(
            ifname="dn42_1234",
            local_lla="fe80::abcd:1234",
            peer_lla="fe80::1",
        )
        assert "[Match]" in result
        assert "Name=dn42_1234" in result
        assert "[Address]" in result
        assert "Address=fe80::abcd:1234/128" in result
        assert "Peer=fe80::1" in result


class TestRenderNmconnection:
    def test_basic(self) -> None:
        result = render_nmconnection_wireguard(
            conn_id="dn42_1234",
            ifname="dn42_1234",
            conn_uuid="test-uuid",
            private_key="PRIVKEY",
            listen_port=51820,
            peer_public_key="PUBKEY",
            endpoint="example.com:51820",
            allowed_ips=["fe80::/64", "fd00::/8"],
            local_lla="fe80::abcd:1234",
            peer_lla="fe80::1",
        )
        assert "[connection]" in result
        assert "id=dn42_1234" in result
        assert "type=wireguard" in result
        assert "[wireguard]" in result
        assert "private-key=PRIVKEY" in result
        assert "listen-port=51820" in result
        assert "peer-routes=false" in result
        assert "[wireguard-peer.PUBKEY]" in result
        assert "endpoint=example.com:51820" in result
        assert "allowed-ips=fe80::/64;fd00::/8;" in result
        assert "[ipv6]" in result
        assert "method=manual" in result
        assert "address1=fe80::abcd:1234/128" in result
        assert "route1=fe80::1/128" in result

    def test_zero_listen_port_omitted(self) -> None:
        result = render_nmconnection_wireguard(
            conn_id="dn42_1234",
            ifname="dn42_1234",
            conn_uuid="test-uuid",
            private_key="PRIVKEY",
            listen_port=0,
            peer_public_key="PUBKEY",
            endpoint="",
            allowed_ips=["fe80::/64"],
            local_lla="fe80::abcd:1234",
            peer_lla="fe80::1",
        )
        assert "listen-port" not in result

    def test_persistent_keepalive(self) -> None:
        result = render_nmconnection_wireguard(
            conn_id="dn42_1234",
            ifname="dn42_1234",
            conn_uuid="test-uuid",
            private_key="PRIVKEY",
            listen_port=51820,
            peer_public_key="PUBKEY",
            endpoint="example.com:51820",
            allowed_ips=["fe80::/64"],
            local_lla="fe80::abcd:1234",
            peer_lla="fe80::1",
            persistent_keepalive=25,
        )
        assert "persistent-keepalive=25" in result

    def test_no_endpoint_omitted(self) -> None:
        result = render_nmconnection_wireguard(
            conn_id="dn42_1234",
            ifname="dn42_1234",
            conn_uuid="test-uuid",
            private_key="PRIVKEY",
            listen_port=51820,
            peer_public_key="PUBKEY",
            endpoint="",
            allowed_ips=["fe80::/64"],
            local_lla="fe80::abcd:1234",
            peer_lla="fe80::1",
        )
        assert "endpoint=" not in result


class TestNmUuidFor:
    def test_deterministic(self) -> None:
        a = nm_uuid_for(node_id="test", ifname="dn42_1234")
        b = nm_uuid_for(node_id="test", ifname="dn42_1234")
        assert a == b

    def test_different_inputs_differ(self) -> None:
        a = nm_uuid_for(node_id="test", ifname="dn42_1234")
        b = nm_uuid_for(node_id="test", ifname="dn42_5678")
        assert a != b


class TestRenderSystemdRoa:
    def test_service(self) -> None:
        result = render_systemd_roa_service(
            roa_parent=Path("/etc/bird"),
            roa_target=Path("/etc/bird/roa_dn42_v6.conf"),
            roa_url="https://dn42.burble.com/roa/dn42_roa_bird2_6.conf",
        )
        assert "[Unit]" in result
        assert "[Service]" in result
        assert "Type=oneshot" in result
        assert "/etc/bird" in result
        assert "curl" in result

    def test_timer(self) -> None:
        result = render_systemd_roa_timer()
        assert "[Timer]" in result
        assert "[Install]" in result
        assert "WantedBy=timers.target" in result
