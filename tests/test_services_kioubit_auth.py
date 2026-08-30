"""Tests for dn42ctl.services.kioubit_auth — signed authentication response verification.

The vectors below are the ones Kioubit publishes alongside the reference
verifiers at https://dn42.g-load.eu/about/authentication-services/, so a failure
here means our verification disagrees with the service itself.
"""

from __future__ import annotations

import base64
import json

import pytest

from dn42ctl.services.kioubit_auth import (
    KioubitAuthError,
    KioubitExpiredError,
    verify_auth_response,
)

PARAMS = (
    "eyJhc24iOiI0MjQyNDIzMDM1IiwidGltZSI6MTY2ODI2NjkyNiwiYWxsb3dlZDQiOiIxNzIuMjIuMTI1LjEyOFwvMjYsMTcyLjIwLjAuODFcLzMyIi"
    "wiYWxsb3dlZDYiOiJmZDYzOjVkNDA6NDdlNTo6XC80OCxmZDQyOmQ0MjpkNDI6ODE6OlwvNjQiLCJtbnQiOiJMQVJFLU1OVCIsImF1dGh0eXBlIjoi"
    "bG9naW5jb2RlIiwiZG9tYWluIjoic3ZjLmJ1cmJsZS5kbjQyIn0="
)
SIGNATURE = (
    "MIGIAkIBAmwz3sQ1vOkH8+8e0NJ8GsUqKSaazIWmYDp60sshlTo7gCAopZOZ6/+tD6s+oEGM1i5mKGbHgK9ROATQLHxUZecCQgCa2N828uNn76z1Y"
    "g63/c7veMVIiK4l1X9TCUepJnJ3mCto+7ogCP+2vQm6GHipSNRF4wnt6tZbir0HZvrqEnRAmA=="
)
DOMAIN = "svc.burble.dn42"
ISSUED_AT = 1668266926


def test_valid_response_yields_identity() -> None:
    identity = verify_auth_response(PARAMS, SIGNATURE, domain=DOMAIN, now=ISSUED_AT)
    assert identity.asn == 4242423035
    assert identity.mntner == "LARE-MNT"
    assert identity.authtype == "logincode"
    assert identity.issued_at == ISSUED_AT


def test_domain_may_be_given_with_scheme() -> None:
    identity = verify_auth_response(PARAMS, SIGNATURE, domain=f"https://{DOMAIN}/", now=ISSUED_AT)
    assert identity.asn == 4242423035


def test_digest_is_stable_per_signature() -> None:
    """auto_peer 用摘要判定重放,同一个签名必须始终得到同一个值。"""
    first = verify_auth_response(PARAMS, SIGNATURE, domain=DOMAIN, now=ISSUED_AT)
    second = verify_auth_response(PARAMS, SIGNATURE, domain=DOMAIN, now=ISSUED_AT)
    assert first.digest == second.digest


def test_tampered_payload_is_rejected() -> None:
    """载荷被改写后签名不再匹配。ASN 由签名担保,不能取自未经校验的 JSON。"""
    payload = json.loads(base64.b64decode(PARAMS))
    payload["asn"] = "4242420000"
    forged = base64.b64encode(json.dumps(payload).encode()).decode()

    with pytest.raises(KioubitAuthError, match="签名校验失败"):
        verify_auth_response(forged, SIGNATURE, domain=DOMAIN, now=ISSUED_AT)


def test_response_for_another_domain_is_rejected() -> None:
    """同一个服务给别站签发的响应不能在本站兑换 session。"""
    with pytest.raises(KioubitAuthError, match="其他域名"):
        verify_auth_response(PARAMS, SIGNATURE, domain="peer.example.com", now=ISSUED_AT)


@pytest.mark.parametrize("skew", [61, -61])
def test_response_outside_the_window_is_rejected(skew: int) -> None:
    with pytest.raises(KioubitExpiredError):
        verify_auth_response(PARAMS, SIGNATURE, domain=DOMAIN, now=ISSUED_AT + skew)


def test_response_at_the_window_edge_is_accepted() -> None:
    identity = verify_auth_response(PARAMS, SIGNATURE, domain=DOMAIN, now=ISSUED_AT + 60)
    assert identity.asn == 4242423035


def test_malformed_base64_is_rejected() -> None:
    with pytest.raises(KioubitAuthError, match="base64"):
        verify_auth_response(PARAMS, "not base64!", domain=DOMAIN, now=ISSUED_AT)


def test_empty_input_is_rejected() -> None:
    with pytest.raises(KioubitAuthError):
        verify_auth_response("", "", domain=DOMAIN, now=ISSUED_AT)
