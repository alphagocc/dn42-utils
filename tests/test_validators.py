from __future__ import annotations

import pytest

from dn42ctl.validators import (
    ValidationError,
    validate_allowed_ips,
    validate_asn,
    validate_babel_type,
    validate_endpoint,
    validate_ipv4_address,
    validate_ipv6_address,
    validate_ipv6_network,
    validate_listen_port,
    validate_net_backend,
    validate_ownnetset_v6,
    validate_pubkey,
    validate_router_id,
    validate_rxcost,
)


class TestValidateListenPort:
    @pytest.mark.parametrize("port", [1, 1000, 51820, 65535])
    def test_valid(self, port: int) -> None:
        assert validate_listen_port(port) == port

    def test_zero_allowed(self) -> None:
        assert validate_listen_port(0, allow_zero=True) == 0

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_listen_port(0)

    @pytest.mark.parametrize("port", [-1, 65536, 100000])
    def test_out_of_range(self, port: int) -> None:
        with pytest.raises(ValidationError):
            validate_listen_port(port)


class TestValidateRxcost:
    @pytest.mark.parametrize("val", [0, 120, 65535])
    def test_valid(self, val: int) -> None:
        assert validate_rxcost(val) == val

    @pytest.mark.parametrize("val", [-1, 65536])
    def test_out_of_range(self, val: int) -> None:
        with pytest.raises(ValidationError):
            validate_rxcost(val)


class TestValidateAsn:
    @pytest.mark.parametrize("val", [1, 4242421234, 999999])
    def test_valid(self, val: int) -> None:
        assert validate_asn(val) == val

    @pytest.mark.parametrize("val", [0, -1, -999])
    def test_invalid(self, val: int) -> None:
        with pytest.raises(ValidationError):
            validate_asn(val)


class TestValidatePubkey:
    def test_valid_44_chars(self) -> None:
        key = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="
        assert validate_pubkey(key) == key

    def test_valid_with_whitespace(self) -> None:
        key = "  YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=  "
        assert validate_pubkey(key) == key.strip()

    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_pubkey("")

    def test_too_short(self) -> None:
        with pytest.raises(ValidationError):
            validate_pubkey("abc")

    @pytest.mark.parametrize(
        ("key", "decoded_bytes"),
        [
            ("A" * 42 + "==", 31),
            ("A" * 44, 33),
            ("QUFB" * 11, 33),
        ],
    )
    def test_rejects_wrong_decoded_length(self, key: str, decoded_bytes: int) -> None:
        """字符数落在 42~44 但解码后不是 32 字节 —— 真实的 wg 一律拒绝这些 key。"""
        import base64

        assert len(base64.b64decode(key, validate=True)) == decoded_bytes
        with pytest.raises(ValidationError, match="长度不对"):
            validate_pubkey(key)

    def test_rejects_non_base64_alphabet(self) -> None:
        with pytest.raises(ValidationError, match="base64"):
            validate_pubkey("!" * 43 + "=")

    def test_invalid_chars(self) -> None:
        with pytest.raises(ValidationError):
            validate_pubkey("!" * 44)


class TestValidateEndpoint:
    @pytest.mark.parametrize(
        "ep",
        ["example.com:51820", "1.2.3.4:12345", "[::1]:51820", "[2001:db8::1]:443"],
    )
    def test_valid(self, ep: str) -> None:
        assert validate_endpoint(ep) == ep

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_endpoint("")

    def test_empty_allowed(self) -> None:
        assert validate_endpoint("", allow_empty=True) == ""

    def test_missing_port(self) -> None:
        with pytest.raises(ValidationError):
            validate_endpoint("example.com")

    def test_port_zero(self) -> None:
        with pytest.raises(ValidationError):
            validate_endpoint("example.com:0")

    def test_port_too_large(self) -> None:
        with pytest.raises(ValidationError):
            validate_endpoint("example.com:99999")


class TestValidateIpv6Address:
    @pytest.mark.parametrize("addr", ["fe80::1", "fd42:4242:1234::1", "::1"])
    def test_valid(self, addr: str) -> None:
        assert validate_ipv6_address(addr) == addr

    def test_with_prefix(self) -> None:
        assert validate_ipv6_address("fe80::1/64") == "fe80::1/64"

    @pytest.mark.parametrize("addr", ["fd00::1/not-a-prefix", "fd00::1/999", "fe80::1/", "fe80::1/64/128"])
    def test_rejects_bad_prefix(self, addr: str) -> None:
        """允许带 /prefix 指的是形状,不是"斜杠后面不管"。"""
        with pytest.raises(ValidationError):
            validate_ipv6_address(addr)

    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_ipv6_address("")

    def test_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_ipv6_address("not-an-ipv6")

    def test_ipv4_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_ipv6_address("192.168.1.1")


class TestValidateIpv4Address:
    @pytest.mark.parametrize("addr", ["172.23.0.1", "10.0.0.1", "255.255.255.255"])
    def test_valid(self, addr: str) -> None:
        assert validate_ipv4_address(addr) == addr

    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_ipv4_address("")

    def test_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_ipv4_address("not-an-ip")


class TestValidateIpv6Network:
    @pytest.mark.parametrize("net", ["fd42:4242:1234::/48", "::/0", "fd00::/8"])
    def test_valid(self, net: str) -> None:
        assert validate_ipv6_network(net) == net

    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_ipv6_network("")

    def test_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_ipv6_network("not-a-cidr")


class TestValidateBabelType:
    @pytest.mark.parametrize("val", ["wired", "wireless", "tunnel"])
    def test_valid(self, val: str) -> None:
        assert validate_babel_type(val) == val

    def test_case_insensitive(self) -> None:
        assert validate_babel_type("TUNNEL") == "tunnel"
        assert validate_babel_type("  Wired  ") == "wired"

    def test_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_babel_type("bridge")


class TestValidateNetBackend:
    def test_networkd(self) -> None:
        assert validate_net_backend("networkd") == "networkd"

    def test_nm(self) -> None:
        assert validate_net_backend("nm") == "nm"

    def test_networkmanager_alias(self) -> None:
        assert validate_net_backend("networkmanager") == "nm"

    def test_case_insensitive(self) -> None:
        assert validate_net_backend("NETWORKD") == "networkd"
        assert validate_net_backend("NM") == "nm"

    def test_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_net_backend("wg-quick")


class TestValidateOwnnetsetV6:
    def test_valid(self) -> None:
        assert validate_ownnetset_v6("[fd42:4242:1234::/48+]") == "[fd42:4242:1234::/48+]"

    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_ownnetset_v6("")

    def test_missing_brackets(self) -> None:
        with pytest.raises(ValidationError):
            validate_ownnetset_v6("fd42:4242:1234::/48+")

    def test_missing_plus(self) -> None:
        with pytest.raises(ValidationError):
            validate_ownnetset_v6("[fd42:4242:1234::/48]")


class TestValidateRouterId:
    def test_valid(self) -> None:
        assert validate_router_id("172.23.0.1") == "172.23.0.1"

    def test_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_router_id("not-an-ip")


class TestValidateAllowedIps:
    def test_single_cidr(self) -> None:
        assert validate_allowed_ips("fd00::/8") == ["fd00::/8"]

    def test_multiple_cidrs(self) -> None:
        result = validate_allowed_ips("fd00::/8,fe80::/64,ff02::/16")
        assert result == ["fd00::/8", "fe80::/64", "ff02::/16"]

    def test_whitespace_handling(self) -> None:
        result = validate_allowed_ips("  fd00::/8 , fe80::/64  ")
        assert result == ["fd00::/8", "fe80::/64"]

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_allowed_ips("")

    def test_normalizes_to_network_address(self) -> None:
        result = validate_allowed_ips("fe80::1/64")
        assert result == ["fe80::/64"]

    def test_all_traffic(self) -> None:
        assert validate_allowed_ips("::/0") == ["::/0"]

    def test_invalid_cidr_raises(self) -> None:
        with pytest.raises(ValidationError, match="不是合法的 IPv6 CIDR"):
            validate_allowed_ips("not-a-cidr")

    def test_invalid_mixed_raises(self) -> None:
        with pytest.raises(ValidationError, match="不是合法的 IPv6 CIDR"):
            validate_allowed_ips("fd00::/8,invalid")

    def test_ipv4_cidr_raises(self) -> None:
        with pytest.raises(ValidationError, match="不是合法的 IPv6 CIDR"):
            validate_allowed_ips("10.0.0.0/8")

    def test_only_whitespace_raises(self) -> None:
        with pytest.raises(ValidationError, match="不能为空"):
            validate_allowed_ips("  ,  , ")


class TestValidateAllowedIpsList:
    def test_empty_list_raises(self) -> None:
        from dn42ctl.validators import validate_allowed_ips_list

        with pytest.raises(ValidationError, match="不能为空"):
            validate_allowed_ips_list([])

    def test_list_with_empty_string_raises(self) -> None:
        from dn42ctl.validators import validate_allowed_ips_list

        with pytest.raises(ValidationError, match="空字符串"):
            validate_allowed_ips_list([""])

    def test_list_with_ipv4_raises(self) -> None:
        from dn42ctl.validators import validate_allowed_ips_list

        with pytest.raises(ValidationError, match="不是合法的 IPv6 CIDR"):
            validate_allowed_ips_list(["10.0.0.0/8"])

    def test_list_with_garbage_raises(self) -> None:
        from dn42ctl.validators import validate_allowed_ips_list

        with pytest.raises(ValidationError, match="不是合法的 IPv6 CIDR"):
            validate_allowed_ips_list(["not-a-cidr"])

    def test_valid_list_passes(self) -> None:
        from dn42ctl.validators import validate_allowed_ips_list

        result = validate_allowed_ips_list(["fe80::/64", "fd00::/8"])
        assert result == ["fe80::/64", "fd00::/8"]


class TestValidateEndpointHost:
    @pytest.mark.parametrize(
        "value",
        ["example.com", "a.b.c.example.com", "node-1.dn42", "1.2.3.4", "2001:db8::1", "fd42:4242:1::1", "localhost"],
    )
    def test_accepts(self, value: str) -> None:
        from dn42ctl.validators import validate_endpoint_host

        assert validate_endpoint_host(value) == value

    def test_strips_whitespace(self) -> None:
        from dn42ctl.validators import validate_endpoint_host

        assert validate_endpoint_host("  example.com  ") == "example.com"

    @pytest.mark.parametrize(
        "value",
        ["example.com:51820", "1.2.3.4:51820", "[2001:db8::1]:51820", "[2001:db8::1]"],
    )
    def test_rejects_port_or_brackets(self, value: str) -> None:
        """端口是每条隧道的 listen_port,不是节点属性。"""
        from dn42ctl.validators import ValidationError, validate_endpoint_host

        with pytest.raises(ValidationError):
            validate_endpoint_host(value)

    @pytest.mark.parametrize("value", ["", "   ", "bad_host!", "-leading.example.com", "a..b"])
    def test_rejects_invalid(self, value: str) -> None:
        from dn42ctl.validators import ValidationError, validate_endpoint_host

        with pytest.raises(ValidationError):
            validate_endpoint_host(value)


class TestSplitAndFormatEndpoint:
    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("example.com:51820", ("example.com", 51820)),
            ("1.2.3.4:51820", ("1.2.3.4", 51820)),
            ("[2001:db8::1]:51820", ("2001:db8::1", 51820)),
        ],
    )
    def test_split(self, endpoint: str, expected: tuple[str, int]) -> None:
        from dn42ctl.validators import split_endpoint

        assert split_endpoint(endpoint) == expected

    def test_split_rejects_garbage(self) -> None:
        from dn42ctl.validators import ValidationError, split_endpoint

        with pytest.raises(ValidationError):
            split_endpoint("no-port-here")

    @pytest.mark.parametrize(
        ("host", "port", "expected"),
        [
            ("example.com", 51820, "example.com:51820"),
            ("1.2.3.4", 51820, "1.2.3.4:51820"),
            ("2001:db8::1", 51820, "[2001:db8::1]:51820"),
            ("[2001:db8::1]", 51820, "[2001:db8::1]:51820"),
        ],
    )
    def test_format(self, host: str, port: int, expected: str) -> None:
        from dn42ctl.validators import format_endpoint

        assert format_endpoint(host, port) == expected

    @pytest.mark.parametrize("endpoint", ["example.com:51820", "1.2.3.4:443", "[2001:db8::1]:51820"])
    def test_round_trip(self, endpoint: str) -> None:
        from dn42ctl.validators import format_endpoint, split_endpoint

        host, port = split_endpoint(endpoint)
        assert format_endpoint(host, port) == endpoint
