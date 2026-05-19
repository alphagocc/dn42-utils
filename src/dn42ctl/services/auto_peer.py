"""Auto-peer orchestration service.

In-memory challenge + session store, registry lookup, and proposal submission.
Process restart drops all challenges/sessions (by design — that's the only way
to forcibly invalidate everything).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ConfigProposal, ManagedNodeStore
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.crypto_verify import verify_pgp, verify_ssh
from dn42ctl.services.node_push import build_peer_add_payload
from dn42ctl.services.proposals import submit_proposal
from dn42ctl.services.registry import (
    PGP_SCHEME,
    SSH_SCHEMES,
    AuthOption,
    RegistryError,
    read_aut_num,
    read_mntner_auth,
    read_pgp_key,
)

CHALLENGE_NAMESPACE = "dn42ctl-autopeer"
_CHALLENGE_TTL_SECONDS = 600.0  # 10 minutes
_SESSION_TTL_SECONDS = 900.0  # 15 minutes


class AutoPeerError(Dn42CtlError):
    """Generic auto-peer flow error (maps to HTTP 400)."""


class AutoPeerExpiredError(AutoPeerError):
    """Challenge / session expired or already consumed (HTTP 410)."""


class AutoPeerSessionError(AutoPeerError):
    """Session token invalid / ASN mismatch (HTTP 403)."""


# --- data structures ---


@dataclass(frozen=True)
class MntnerOptions:
    name: str
    auth_options: list[AuthOption]


@dataclass(frozen=True)
class LookupResult:
    asn: int
    mntners: list[MntnerOptions]


@dataclass(frozen=True)
class ChallengeIssued:
    challenge_id: str
    nonce_hex: str
    namespace: str
    scheme: str
    expires_at: float


@dataclass(frozen=True)
class VerifyResult:
    peer_session_token: str
    verified_asn: int
    verified_mntner: str
    expires_at: float


@dataclass(frozen=True)
class SubmitResult:
    proposal: ConfigProposal
    our_node_id: str


# Internal records (not part of the public dataclass surface)


@dataclass
class _Challenge:
    id: str
    nonce: bytes
    namespace: str
    asn: int
    mntner: str
    scheme: str
    auth_raw: str  # full auth-line value (without `auth:` prefix)
    fingerprint: str | None
    expires_at: float  # time.monotonic() deadline


@dataclass
class _Session:
    token: str
    asn: int
    mntner: str
    expires_at: float


_lock = threading.Lock()
_challenges: dict[str, _Challenge] = {}
_sessions: dict[str, _Session] = {}


def _now() -> float:
    return time.monotonic()


def _purge_expired_locked(now: float) -> None:
    expired_c = [k for k, v in _challenges.items() if v.expires_at <= now]
    for k in expired_c:
        _challenges.pop(k, None)
    expired_s = [k for k, v in _sessions.items() if v.expires_at <= now]
    for k in expired_s:
        _sessions.pop(k, None)


def reset_state() -> None:
    """Clear all in-memory challenges and sessions. Tests call this between cases."""
    with _lock:
        _challenges.clear()
        _sessions.clear()


# --- step 1: lookup ---


def start_lookup(*, config: AppConfig, asn: int) -> LookupResult:
    """Read aut-num + mntner files and return supported auth options per mntner."""
    if asn <= 0:
        raise AutoPeerError(f"ASN 必须为正整数: {asn}")
    registry_path = config.dn42_registry_path
    if not registry_path:
        # Shouldn't be reached if the API guard runs first, but be defensive.
        raise AutoPeerError("auto-peer 未启用 (dn42_registry_path 未配置)")

    mntner_names = read_aut_num(registry_path, asn)
    mntner_results: list[MntnerOptions] = []
    for name in mntner_names:
        try:
            opts = read_mntner_auth(registry_path, name)
        except RegistryError:
            # Skip mntners whose files are broken/missing; report what we can.
            continue
        mntner_results.append(MntnerOptions(name=name, auth_options=opts))
    if not any(m.auth_options for m in mntner_results):
        raise AutoPeerError(f"AS{asn} 的 mntner 没有可用的 auth 方式（仅支持 SSH / PGP）")
    return LookupResult(asn=asn, mntners=mntner_results)


# --- step 2: challenge ---


def start_challenge(*, config: AppConfig, asn: int, mntner: str, auth_index: int) -> ChallengeIssued:
    """Issue a single-use challenge bound to (asn, mntner, auth_index)."""
    registry_path = config.dn42_registry_path
    if not registry_path:
        raise AutoPeerError("auto-peer 未启用 (dn42_registry_path 未配置)")
    # Verify mntner is actually one of the AS's mnt-by entries — prevents
    # a caller from challenging against arbitrary mntners.
    mnt_by = read_aut_num(registry_path, asn)
    if mntner not in mnt_by:
        raise AutoPeerError(f"{mntner} 不在 AS{asn} 的 mnt-by 列表中")
    options = read_mntner_auth(registry_path, mntner)
    if auth_index < 0 or auth_index >= len(options):
        raise AutoPeerError(f"auth_index 越界: {auth_index}")
    option = options[auth_index]

    if option.scheme in SSH_SCHEMES:
        kind = "ssh"
    elif option.scheme == PGP_SCHEME:
        kind = "pgp"
    else:  # pragma: no cover — read_mntner_auth already filters
        raise AutoPeerError(f"不支持的 auth 方案: {option.scheme}")

    challenge_id = secrets.token_urlsafe(16)
    nonce = secrets.token_bytes(32)
    now = _now()
    expires_at = now + _CHALLENGE_TTL_SECONDS

    with _lock:
        _purge_expired_locked(now)
        _challenges[challenge_id] = _Challenge(
            id=challenge_id,
            nonce=nonce,
            namespace=CHALLENGE_NAMESPACE,
            asn=asn,
            mntner=mntner,
            scheme=kind,
            auth_raw=option.raw,
            fingerprint=option.fingerprint,
            expires_at=expires_at,
        )

    return ChallengeIssued(
        challenge_id=challenge_id,
        nonce_hex=nonce.hex(),
        namespace=CHALLENGE_NAMESPACE,
        scheme=kind,
        expires_at=expires_at,
    )


# --- step 3: verify ---


def verify_challenge(*, config: AppConfig, challenge_id: str, signature: str) -> VerifyResult:
    """Validate the user's signature against the stored challenge.

    On success: burn the challenge, issue a peer_session_token.
    On failure: leave the challenge in place (until TTL) so the user can retry.
    On expired/missing challenge: raise AutoPeerExpiredError.
    """
    registry_path = config.dn42_registry_path
    if not registry_path:
        raise AutoPeerError("auto-peer 未启用 (dn42_registry_path 未配置)")
    if not signature or not signature.strip():
        raise AutoPeerError("signature 不能为空")

    now = _now()
    with _lock:
        _purge_expired_locked(now)
        challenge = _challenges.get(challenge_id)
    if challenge is None:
        raise AutoPeerExpiredError("挑战不存在或已过期")
    if challenge.expires_at <= now:
        raise AutoPeerExpiredError("挑战已过期")

    if challenge.scheme == "ssh":
        # auth_raw is the full ssh pubkey line ("ssh-ed25519 AAAA... comment").
        identity = f"dn42@AS{challenge.asn}-{challenge.mntner}"
        ok = verify_ssh(
            message=challenge.nonce.hex().encode("ascii"),
            signature=signature,
            allowed_pubkey=challenge.auth_raw,
            namespace=challenge.namespace,
            identity=identity,
        )
    elif challenge.scheme == "pgp":
        if not challenge.fingerprint:
            raise AutoPeerError("PGP 挑战缺少 fingerprint")
        try:
            ascii_key = read_pgp_key(registry_path, challenge.fingerprint)
        except RegistryError as exc:
            raise AutoPeerError(str(exc)) from exc
        ok = verify_pgp(
            message=challenge.nonce.hex().encode("ascii"),
            signature=signature,
            ascii_key=ascii_key,
        )
    else:  # pragma: no cover
        raise AutoPeerError(f"未知 challenge scheme: {challenge.scheme}")

    if not ok:
        raise AutoPeerError("签名校验失败")

    session_token = secrets.token_urlsafe(32)
    session_expires = now + _SESSION_TTL_SECONDS
    with _lock:
        _challenges.pop(challenge_id, None)
        _sessions[session_token] = _Session(
            token=session_token,
            asn=challenge.asn,
            mntner=challenge.mntner,
            expires_at=session_expires,
        )

    return VerifyResult(
        peer_session_token=session_token,
        verified_asn=challenge.asn,
        verified_mntner=challenge.mntner,
        expires_at=session_expires,
    )


# --- step 4: submit ---


def _resolve_session(token: str) -> _Session:
    if not token:
        raise AutoPeerSessionError("缺少 peer-session token")
    now = _now()
    with _lock:
        _purge_expired_locked(now)
        session = _sessions.get(token)
    if session is None or session.expires_at <= now:
        raise AutoPeerExpiredError("peer-session 已过期或无效")
    return session


def submit_peer(
    *,
    config: AppConfig,
    db_path: Path,
    session_token: str,
    wg_public_key: str,
    endpoint: str,
    peer_lla: str,
    net_backend: str = "networkd",
    listen_port: int | None = None,
) -> SubmitResult:
    """Resolve the session, look up the self node_id, submit a peer_add proposal."""
    session = _resolve_session(session_token)

    db = Database.open(db_path)
    try:
        self_node = ManagedNodeStore(db.connection).get_self()
    finally:
        db.close()
    if self_node is None:
        raise AutoPeerError("self 节点尚未注册：先启动一次 `dn42ctl serve` 让 bootstrap 跑完")

    payload = build_peer_add_payload(
        peer_kind="bgp",
        peer={
            "peer_asn": session.asn,
            "peer_public_key": wg_public_key,
            "endpoint": endpoint,
            "peer_lla": peer_lla,
            "net_backend": net_backend,
            "listen_port": listen_port,
        },
    )

    proposal = submit_proposal(
        db_path=db_path,
        node_id=self_node.node_id,
        source="push",
        kind="peer_add",
        payload=payload,
        config=None,
    )

    # Single-use: consume the session on successful submission.
    with _lock:
        _sessions.pop(session_token, None)

    return SubmitResult(proposal=proposal, our_node_id=self_node.node_id)
