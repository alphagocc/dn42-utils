"""Auto-peer orchestration service.

In-memory session store and proposal submission. Process restart drops all
sessions (by design — that's the only way to forcibly invalidate everything).
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
from dn42ctl.services.kioubit_auth import MAX_AGE_SECONDS, KioubitIdentity
from dn42ctl.services.node_push import build_peer_add_payload
from dn42ctl.services.proposals import submit_proposal

_SESSION_TTL_SECONDS = 900.0  # 15 minutes
# A verified response stays replayable until its own freshness window closes, so
# remembering consumed signatures for twice that span covers the whole exposure
# regardless of which direction the clocks are skewed.
_CONSUMED_TTL_SECONDS = MAX_AGE_SECONDS * 2


class AutoPeerError(Dn42CtlError):
    """Generic auto-peer flow error (maps to HTTP 400)."""


class AutoPeerExpiredError(AutoPeerError):
    """Session expired or already consumed (HTTP 410)."""


class AutoPeerSessionError(AutoPeerError):
    """Session token invalid / ASN mismatch (HTTP 403)."""


@dataclass(frozen=True)
class SessionIssued:
    peer_session_token: str
    verified_asn: int
    verified_mntner: str
    expires_at: float


@dataclass(frozen=True)
class PeerTarget:
    node_id: str
    name: str
    endpoint_host: str | None


@dataclass(frozen=True)
class SubmitResult:
    proposal: ConfigProposal
    node_id: str
    node_name: str


@dataclass
class _Session:
    token: str
    asn: int
    mntner: str
    expires_at: float
    in_flight: bool = False


_lock = threading.Lock()
_sessions: dict[str, _Session] = {}
_consumed: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def _purge_expired_locked(now: float) -> None:
    for token in [k for k, v in _sessions.items() if v.expires_at <= now]:
        _sessions.pop(token, None)
    for digest in [k for k, deadline in _consumed.items() if deadline <= now]:
        _consumed.pop(digest, None)


def reset_state() -> None:
    """Clear all in-memory sessions. Tests call this between cases."""
    with _lock:
        _sessions.clear()
        _consumed.clear()


# --- step 1: exchange a verified identity for a peer session ---


def open_session(identity: KioubitIdentity) -> SessionIssued:
    """Issue a peer-session token for an already-verified identity.

    The signature digest is burned so a response captured from the browser's URL
    cannot be handed in twice while it is still fresh.
    """
    now = _now()
    expires_at = now + _SESSION_TTL_SECONDS
    token = secrets.token_urlsafe(32)

    with _lock:
        _purge_expired_locked(now)
        if identity.digest in _consumed:
            raise AutoPeerExpiredError("该认证响应已被使用，请重新认证")
        _consumed[identity.digest] = now + _CONSUMED_TTL_SECONDS
        _sessions[token] = _Session(
            token=token,
            asn=identity.asn,
            mntner=identity.mntner,
            expires_at=expires_at,
        )

    return SessionIssued(
        peer_session_token=token,
        verified_asn=identity.asn,
        verified_mntner=identity.mntner,
        expires_at=expires_at,
    )


# --- step 2: choose a node and submit ---


def list_peer_targets(*, db_path: Path) -> list[PeerTarget]:
    """开放给公共页面的节点。运维没开过任何节点时返回空列表。"""
    db = Database.open(db_path)
    try:
        nodes = ManagedNodeStore(db.connection).list_auto_peer()
    finally:
        db.close()
    return [PeerTarget(node_id=n.node_id, name=n.name, endpoint_host=n.endpoint_host) for n in nodes]


def _resolve_target(db_path: Path, node_id: str) -> tuple[str, str]:
    """把请求里的 node_id 解析成 (node_id, name)。

    判定只认 `list_auto_peer` 返回的那批节点。提交与列表因此共用同一处规则,运维关掉开关
    之后停留在旧页面上的人也提交不进来。
    """
    for target in list_peer_targets(db_path=db_path):
        if target.node_id == node_id:
            return target.node_id, target.name
    raise AutoPeerError(f"节点不接受 auto-peer 请求: {node_id}")


def _claim_session(token: str) -> _Session:
    """认领 session。同一个 token 的并发提交只有第一个能拿到。"""
    if not token:
        raise AutoPeerSessionError("缺少 peer-session token")
    now = _now()
    with _lock:
        _purge_expired_locked(now)
        session = _sessions.get(token)
        if session is None or session.expires_at <= now:
            raise AutoPeerExpiredError("peer-session 已过期或无效")
        if session.in_flight:
            raise AutoPeerError("该 peer-session 正在处理中，请稍候重试")
        session.in_flight = True
        return session


def _release_session(token: str) -> None:
    with _lock:
        session = _sessions.get(token)
        if session is not None:
            session.in_flight = False


def submit_peer(
    *,
    config: AppConfig,
    db_path: Path,
    session_token: str,
    node_id: str,
    wg_public_key: str,
    endpoint: str,
    peer_lla: str,
    net_backend: str = "networkd",
    listen_port: int | None = None,
) -> SubmitResult:
    """Resolve the session and the chosen node, submit a peer_add proposal."""
    session = _claim_session(session_token)

    try:
        target_id, target_name = _resolve_target(db_path, node_id)

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
            node_id=target_id,
            source="push",
            kind="peer_add",
            payload=payload,
            config=None,
        )
    except Exception:
        # 失败保留 session,让用户改字段重交。
        _release_session(session_token)
        raise

    with _lock:
        _sessions.pop(session_token, None)

    return SubmitResult(proposal=proposal, node_id=target_id, node_name=target_name)
