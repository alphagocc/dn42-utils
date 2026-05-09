from __future__ import annotations

import ipaddress
import re

from dn42ctl.constants import BABEL_VALID_TYPES, MAX_PORT


class ValidationError(ValueError):
    pass


_WG_PUBKEY_RE = re.compile(r"^[A-Za-z0-9+/]{42,44}={0,2}$")
_ENDPOINT_RE = re.compile(r"^(\[.+\]|[^:]+):(\d+)$")


def validate_listen_port(value: int, *, allow_zero: bool = False) -> int:
    lo = 0 if allow_zero else 1
    if value < lo or value > MAX_PORT:
        raise ValidationError(f"ListenPort 超出范围 ({lo}-{MAX_PORT}): {value}")
    return value


def validate_rxcost(value: int) -> int:
    if value < 0 or value > MAX_PORT:
        raise ValidationError(f"rxcost 超出范围 (0-{MAX_PORT}): {value}")
    return value


def validate_asn(value: int) -> int:
    if value <= 0:
        raise ValidationError(f"ASN 必须是正整数: {value}")
    return value


def validate_pubkey(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError("公钥不能为空")
    if not _WG_PUBKEY_RE.match(value):
        raise ValidationError(f"公钥格式不合法 (base64, 需40~44字符): {value!r}")
    return value


def validate_endpoint(value: str, *, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value:
        if allow_empty:
            return value
        raise ValidationError("Endpoint 不能为空")
    m = _ENDPOINT_RE.match(value)
    if not m:
        raise ValidationError(
            f"Endpoint 格式错误: 需要 host:port 或 [IPv6]:port 形式: {value!r}"
        )
    port = int(m.group(2))
    if not (1 <= port <= MAX_PORT):
        raise ValidationError(f"Endpoint 端口超出范围 (1-{MAX_PORT}): {port}")
    return value


def validate_ipv6_address(value: str, *, field_name: str = "IPv6 地址") -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{field_name} 不能为空")
    addr_part = value.split("/", 1)[0]
    try:
        ipaddress.IPv6Address(addr_part)
    except ValueError as exc:
        raise ValidationError(f"不是合法的 IPv6 地址: {value!r}") from exc
    return value


def validate_ipv4_address(value: str, *, field_name: str = "IPv4 地址") -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{field_name} 不能为空")
    try:
        ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValidationError(f"不是合法的 IPv4 地址: {value!r}") from exc
    return value


def validate_ipv6_network(value: str, *, field_name: str = "IPv6 前缀") -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{field_name} 不能为空")
    try:
        ipaddress.IPv6Network(value, strict=False)
    except ValueError as exc:
        raise ValidationError(f"不是合法的 IPv6 CIDR 前缀: {value!r}") from exc
    return value


def validate_babel_type(value: str) -> str:
    value = value.strip().lower()
    if value not in BABEL_VALID_TYPES:
        raise ValidationError(
            f"type 必须是 {', '.join(BABEL_VALID_TYPES)} 之一: {value!r}"
        )
    return value


def validate_net_backend(value: str) -> str:
    backend = value.strip().lower()
    if backend == "networkd":
        return "networkd"
    if backend in {"nm", "networkmanager"}:
        return "nm"
    raise ValidationError(f"net_backend 必须是 networkd 或 nm: {value!r}")


def validate_ownnetset_v6(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError("OWNNETSETv6 不能为空")
    if not (value.startswith("[") and value.endswith("]") and "+" in value):
        raise ValidationError(
            f"OWNNETSETv6 格式不合法，需要形如 [prefix+/...] 的格式: {value!r}"
        )
    return value


def validate_router_id(value: str) -> str:
    return validate_ipv4_address(value, field_name="Router ID")
