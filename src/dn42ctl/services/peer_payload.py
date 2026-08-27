"""Validate untrusted peer payloads before they reach the service layer.

`config_proposals.payload_json` and `node_reports.payload_json` are arbitrary JSON
written by whoever holds a node token; nothing between the wire and the DB imposes a
schema. The service layer only checks `listen_port` / `net_backend` / `allowed_ips`,
so every other field would otherwise land in the DB verbatim and be rendered into the
spoke's `bird.conf` on its next pull — where `include "<peers_dir>/*";` turns one
malformed peer into a total BIRD config failure for that node.

Every parse error surfaces as `Dn42CtlError`, which callers already map to HTTP 400.
Letting `KeyError` / `ValueError` / `OverflowError` escape instead would produce a 500
with no indication of which field was bad, and on the WebSocket path it tears down the
agent connection outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.validators import (
    ValidationError,
    validate_asn,
    validate_babel_type,
    validate_endpoint,
    validate_ipv6_address,
    validate_listen_port,
    validate_net_backend,
    validate_pubkey,
    validate_rxcost,
)

DEFAULT_NET_BACKEND = "networkd"


@dataclass(frozen=True)
class BgpPeerPayload:
    peer_asn: int
    peer_public_key: str
    endpoint: str
    peer_lla: str
    net_backend: str
    listen_port: int | None


@dataclass(frozen=True)
class IbgpPeerPayload:
    name: str
    peer_ip: str
    has_wg: bool
    peer_public_key: str | None
    endpoint: str | None
    peer_lla: str | None
    net_backend: str
    babel_rxcost: int
    babel_type: str
    listen_port: int | None


def _require(peer: dict[str, Any], field: str) -> Any:  # noqa: ANN401 — 值的类型由各字段的解析器决定
    if field not in peer:
        raise Dn42CtlError(f"peer payload 缺少必填字段: {field}")
    return peer[field]


def _as_int(value: Any, field: str) -> int:  # noqa: ANN401
    # bool 是 int 的子类,`has_wg: true` 之类的笔误会被静默当成 1;浮点则会被静默截断。
    if isinstance(value, bool) or not isinstance(value, int):
        raise Dn42CtlError(f"{field} 必须是整数, 收到 {value!r}")
    return value


def _as_str(value: Any, field: str) -> str:  # noqa: ANN401
    if not isinstance(value, str):
        raise Dn42CtlError(f"{field} 必须是字符串, 收到 {value!r}")
    return value


def _as_bool(value: Any, field: str) -> bool:  # noqa: ANN401
    if not isinstance(value, bool):
        raise Dn42CtlError(f"{field} 必须是布尔值, 收到 {value!r}")
    return value


def _checked(field: str, fn, value):  # noqa: ANN001, ANN202 — 泛化包装,返回类型随 fn
    try:
        return fn(value)
    except ValidationError as exc:
        raise Dn42CtlError(f"{field}: {exc}") from exc


def _optional_listen_port(peer: dict[str, Any]) -> int | None:
    raw = peer.get("listen_port")
    if raw is None:
        return None
    return _checked("listen_port", lambda v: validate_listen_port(v, allow_zero=True), _as_int(raw, "listen_port"))


def _net_backend(peer: dict[str, Any]) -> str:
    raw = peer.get("net_backend") or DEFAULT_NET_BACKEND
    return _checked("net_backend", validate_net_backend, _as_str(raw, "net_backend"))


def parse_bgp_peer(peer: dict[str, Any]) -> BgpPeerPayload:
    return BgpPeerPayload(
        peer_asn=_checked("peer_asn", validate_asn, _as_int(_require(peer, "peer_asn"), "peer_asn")),
        peer_public_key=_checked(
            "peer_public_key", validate_pubkey, _as_str(_require(peer, "peer_public_key"), "peer_public_key")
        ),
        endpoint=_checked(
            "endpoint",
            lambda v: validate_endpoint(v, allow_empty=True),
            _as_str(peer.get("endpoint") or "", "endpoint"),
        ),
        peer_lla=_checked(
            "peer_lla",
            lambda v: validate_ipv6_address(v, field_name="peer_lla"),
            _as_str(_require(peer, "peer_lla"), "peer_lla"),
        ),
        net_backend=_net_backend(peer),
        listen_port=_optional_listen_port(peer),
    )


def parse_ibgp_peer(peer: dict[str, Any]) -> IbgpPeerPayload:
    """Parse an iBGP peer payload.

    `has_wg=False` peers carry no tunnel at all, so the WireGuard fields are dropped
    rather than validated — a payload built from a DB row carries `""` for those
    columns, and rejecting it would break a legitimate round-trip. When a tunnel *is*
    present they are mandatory: an empty public key renders a bare `PublicKey=` line
    that systemd-networkd silently refuses to bring up.
    """
    name = _as_str(_require(peer, "name"), "name")
    if not name.strip():
        raise Dn42CtlError("name 不能为空")
    has_wg = _as_bool(_require(peer, "has_wg"), "has_wg")

    peer_public_key: str | None = None
    peer_lla: str | None = None
    if has_wg:
        peer_public_key = _checked(
            "peer_public_key", validate_pubkey, _as_str(_require(peer, "peer_public_key"), "peer_public_key")
        )
        peer_lla = _checked(
            "peer_lla",
            lambda v: validate_ipv6_address(v, field_name="peer_lla"),
            _as_str(_require(peer, "peer_lla"), "peer_lla"),
        )

    return IbgpPeerPayload(
        name=name,
        peer_ip=_checked(
            "peer_ip",
            lambda v: validate_ipv6_address(v, field_name="peer_ip"),
            _as_str(_require(peer, "peer_ip"), "peer_ip"),
        ),
        has_wg=has_wg,
        peer_public_key=peer_public_key,
        endpoint=_checked(
            "endpoint",
            lambda v: validate_endpoint(v, allow_empty=True),
            _as_str(peer.get("endpoint") or "", "endpoint"),
        ),
        peer_lla=peer_lla,
        net_backend=_net_backend(peer),
        babel_rxcost=_checked("babel_rxcost", validate_rxcost, _as_int(_require(peer, "babel_rxcost"), "babel_rxcost")),
        babel_type=_checked("babel_type", validate_babel_type, _as_str(_require(peer, "babel_type"), "babel_type")),
        listen_port=_optional_listen_port(peer),
    )


def parse_bgp_key(key: dict[str, Any]) -> int:
    return _checked("peer_asn", validate_asn, _as_int(_require(key, "peer_asn"), "peer_asn"))


def parse_ibgp_key(key: dict[str, Any]) -> str:
    name = _as_str(_require(key, "name"), "name")
    if not name.strip():
        raise Dn42CtlError("name 不能为空")
    return name


__all__ = [
    "BgpPeerPayload",
    "IbgpPeerPayload",
    "parse_bgp_key",
    "parse_bgp_peer",
    "parse_ibgp_key",
    "parse_ibgp_peer",
]
