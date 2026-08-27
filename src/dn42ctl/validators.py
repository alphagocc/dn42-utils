from __future__ import annotations

import base64
import binascii
import ipaddress
import re

from dn42ctl.constants import BABEL_VALID_TYPES, MAX_ASN, MAX_PORT, WG_KEY_BYTES


class ValidationError(ValueError):
    pass


_ENDPOINT_RE = re.compile(r"^(\[.+\]|[^:]+):(\d+)$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)


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
    if value > MAX_ASN:
        raise ValidationError(f"ASN 超出 32 位范围 (1-{MAX_ASN}): {value}")
    return value


def validate_pubkey(value: str) -> str:
    """WireGuard X25519 公钥:标准 base64,解码后恰好 32 字节。

    按解码长度而不是字符数校验。字符数放不出这个约束——42~46 个 base64 字符能塞进
    31 到 33 字节,而 `wg` 对非 32 字节的 key 一律报 "Key is not the correct length
    or format"。
    """
    value = value.strip()
    if not value:
        raise ValidationError("公钥不能为空")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValidationError(f"公钥不是合法的 base64: {value!r}") from exc
    if len(raw) != WG_KEY_BYTES:
        raise ValidationError(f"公钥长度不对 (解码后需 {WG_KEY_BYTES} 字节, 实际 {len(raw)}): {value!r}")
    return value


def validate_endpoint(value: str, *, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value:
        if allow_empty:
            return value
        raise ValidationError("Endpoint 不能为空")
    m = _ENDPOINT_RE.match(value)
    if not m:
        raise ValidationError(f"Endpoint 格式错误: 需要 host:port 或 [IPv6]:port 形式: {value!r}")
    port = int(m.group(2))
    if not (1 <= port <= MAX_PORT):
        raise ValidationError(f"Endpoint 端口超出范围 (1-{MAX_PORT}): {port}")
    return value


def validate_ipv6_address(value: str, *, field_name: str = "IPv6 地址") -> str:
    """合法 IPv6 地址,允许带 `/prefix`。

    带前缀时用 IPv6Interface 整串解析:宽松的是"接受哪种形状",不是"接受什么内容"。
    早先在 `/` 处截断只验前半段,于是 `fd00::1/not-a-prefix` 会被原样写进 bird.conf
    的 neighbor 行与 networkd 的 Peer=。
    """
    value = value.strip()
    if not value:
        raise ValidationError(f"{field_name} 不能为空")
    try:
        if "/" in value:
            ipaddress.IPv6Interface(value)
        else:
            ipaddress.IPv6Address(value)
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
        raise ValidationError(f"type 必须是 {', '.join(BABEL_VALID_TYPES)} 之一: {value!r}")
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
        raise ValidationError(f"OWNNETSETv6 格式不合法，需要形如 [prefix+/...] 的格式: {value!r}")
    return value


def validate_allowed_ips(value: str) -> list[str]:
    items = [s.strip() for s in value.split(",")]
    items = [s for s in items if s]
    if not items:
        raise ValidationError("AllowedIPs 不能为空，至少需要一个合法的 IPv6 CIDR")
    result: list[str] = []
    for item in items:
        try:
            net = ipaddress.IPv6Network(item, strict=False)
        except ValueError as exc:
            raise ValidationError(f"不是合法的 IPv6 CIDR: {item!r}") from exc
        result.append(str(net))
    return result


def validate_allowed_ips_list(value: list[str]) -> list[str]:
    if not value:
        raise ValidationError("AllowedIPs 不能为空，至少需要一个合法的 IPv6 CIDR")
    result: list[str] = []
    for item in value:
        item = item.strip()
        if not item:
            raise ValidationError("AllowedIPs 包含空字符串")
        try:
            net = ipaddress.IPv6Network(item, strict=False)
        except ValueError as exc:
            raise ValidationError(f"不是合法的 IPv6 CIDR: {item!r}") from exc
        result.append(str(net))
    return result


def validate_router_id(value: str) -> str:
    return validate_ipv4_address(value, field_name="Router ID")


def validate_endpoint_host(value: str) -> str:
    """节点的公网可达 host：DNS 名 / IPv4 / IPv6 字面量（裸写，不带方括号）。

    **拒绝带端口。** 端口是每条隧道各自的 listen_port，不是节点属性——一个节点对不同
    对端可以监听不同端口，也可能在 NAT 后被映射成完全不同的端口。
    """
    value = value.strip()
    if not value:
        raise ValidationError("endpoint_host 不能为空")
    if value.startswith("["):
        raise ValidationError(f"endpoint_host 是裸主机名/地址，不要带方括号或端口: {value!r}")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return value  # 合法的 IPv4 / IPv6 字面量
    # 不是 IP 字面量。此时出现 ':' 只可能是误带了端口(或畸形的 IPv6)。
    if ":" in value:
        raise ValidationError(f"endpoint_host 不能包含端口: {value!r}")
    if not _HOSTNAME_RE.match(value):
        raise ValidationError(f"endpoint_host 不是合法的主机名: {value!r}")
    return value


def split_endpoint(value: str) -> tuple[str, int]:
    """`host:port` / `[v6]:port` -> (host, port)。host 不含方括号。"""
    m = _ENDPOINT_RE.match(value.strip())
    if not m:
        raise ValidationError(f"Endpoint 格式错误: 需要 host:port 或 [IPv6]:port 形式: {value!r}")
    host = m.group(1)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(m.group(2))


def format_endpoint(host: str, port: int) -> str:
    """(host, port) -> endpoint。IPv6 字面量自动加方括号。"""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        needs_brackets = isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        needs_brackets = False
    return f"[{host}]:{port}" if needs_brackets else f"{host}:{port}"
