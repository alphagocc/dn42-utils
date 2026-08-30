"""Kioubit dn42 authentication service response verification.

The browser is redirected to `https://dn42.g-load.eu/auth/` and comes back with
two query parameters: `params` (base64 of a JSON payload) and `signature`
(base64 of an ASN.1 ECDSA signature over the `params` string as received).
Verification is pure local crypto — the server makes no outbound request, which
is what lets `dn42ctl-server.service` keep `IPAddressDeny=any`.

The signature covers the whole payload including its `domain` field, so
comparing `domain` against this deployment's own hostname is what stops a
response minted for another site from being replayed here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from dn42ctl.services.core import Dn42CtlError
from dn42ctl.validators import validate_asn

# https://dn42.g-load.eu/auth/assets/public_key.pem — EC P-521 (secp521r1).
# Pinned rather than fetched: the server has no egress, and a key fetched at
# runtime would be trusted on the strength of the same TLS chain an attacker
# would have to break anyway.
KIOUBIT_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGbMBAGByqGSM49AgEGBSuBBAAjA4GGAAQA4DCOlR4g94udKvu9+zKbwqxV/rjg
7g/Ij91TnEqUhIBi3LBjBoykcSFnWBtXgnmrj0WxBt4vAj7YO5jCQZJMVeIALCcS
Fkn447bsp6NdQzxTMQ8UYTPX/g7AzxEDOVtXfSBo/BiZF9LYPmm0zwk+y7ZmR8Kf
02hEtJbbjN/GoD6Mq1c=
-----END PUBLIC KEY-----
"""

AUTH_URL = "https://dn42.g-load.eu/auth/"

# Kioubit signs a wall-clock timestamp; this is the accepted skew in either
# direction, matching the value used by the service's own reference verifiers.
MAX_AGE_SECONDS = 60.0


class KioubitAuthError(Dn42CtlError):
    """Malformed, unsigned, or wrong-domain authentication response (HTTP 400)."""


class KioubitExpiredError(KioubitAuthError):
    """Response timestamp outside the accepted window (HTTP 410)."""


@dataclass(frozen=True)
class KioubitIdentity:
    asn: int
    mntner: str
    authtype: str
    issued_at: float
    digest: str


def _load_public_key() -> ec.EllipticCurvePublicKey:
    key = serialization.load_pem_public_key(KIOUBIT_PUBLIC_KEY_PEM)
    if not isinstance(key, ec.EllipticCurvePublicKey):  # pragma: no cover — constant input
        raise KioubitAuthError("内置公钥不是 EC 公钥")
    return key


_public_key = _load_public_key()


def _b64decode(value: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KioubitAuthError(f"{field} 不是合法的 base64") from exc


def _pick_mntner(payload: dict[str, Any]) -> str:
    """Prefer the maintainer that actually authenticated over the ASN's full list."""
    effective = payload.get("effective_mnt")
    if isinstance(effective, str) and effective.strip():
        return effective.strip()
    # `mnt` carries every maintainer of the ASN; older responses put a single
    # name there as a plain string.
    mnt = payload.get("mnt")
    if isinstance(mnt, str) and mnt.strip():
        return mnt.strip()
    if isinstance(mnt, list):
        for item in mnt:
            if isinstance(item, str) and item.strip():
                return item.strip()
    raise KioubitAuthError("认证响应缺少 mntner")


def verify_auth_response(
    params: str,
    signature: str,
    *,
    domain: str,
    now: float | None = None,
) -> KioubitIdentity:
    """Verify a signed authentication response and return the identity it asserts.

    `params` must be passed exactly as received: the signature covers that string,
    not the JSON it decodes to.
    """
    if not params or not signature:
        raise KioubitAuthError("params 与 signature 都不能为空")

    signature_bytes = _b64decode(signature, field="signature")
    try:
        _public_key.verify(signature_bytes, params.encode("utf-8"), ec.ECDSA(hashes.SHA512()))
    except InvalidSignature as exc:
        raise KioubitAuthError("签名校验失败") from exc

    try:
        payload = json.loads(_b64decode(params, field="params"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KioubitAuthError("认证响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise KioubitAuthError("认证响应不是 JSON 对象")

    expected_domain = domain.removeprefix("https://").removeprefix("http://").rstrip("/")
    if payload.get("domain") != expected_domain:
        raise KioubitAuthError("认证响应属于其他域名")

    try:
        issued_at = float(payload["time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise KioubitAuthError("认证响应缺少合法的 time 字段") from exc
    current = time.time() if now is None else now
    if abs(current - issued_at) > MAX_AGE_SECONDS:
        raise KioubitExpiredError("认证响应已过期，请重新认证")

    raw_asn = payload.get("asn")
    try:
        # ValidationError 继承自 ValueError,与 int() 的失败共用一个分支。
        asn = validate_asn(int(raw_asn))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise KioubitAuthError(f"认证响应中的 ASN 不合法: {raw_asn!r}") from exc

    authtype = payload.get("authtype")
    return KioubitIdentity(
        asn=asn,
        mntner=_pick_mntner(payload),
        authtype=authtype if isinstance(authtype, str) else "",
        issued_at=issued_at,
        digest=hashlib.sha256(signature_bytes).hexdigest(),
    )
