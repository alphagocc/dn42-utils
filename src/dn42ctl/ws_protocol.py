"""WebSocket envelope shared by the hub and the resident node agent.

Pure data + (de)serialization, no I/O — see `docs/architecture/sync_ws_protocol.md`
for the full protocol, message catalogue and lifecycle.

The wire format is one JSON object per text frame:

    {"v": 1, "type": "ping", "id": "<uuid4 hex>", "re": null,
     "ts": "2026-07-29T12:00:00+00:00", "payload": {}}
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Not version negotiation. The project ships hub and spokes together (see
# docs/spec.md), so a mismatched `v` is a deployment error: we answer with
# error{version_mismatch} and close 4008 so the skew fails loudly instead of
# silently misbehaving on defaulted fields. Do NOT grow compat shims off this.
PROTOCOL_VERSION = 1

# Frame size ceiling, below uvicorn's 16 MiB default so an oversized payload
# surfaces as our own error rather than a transport-level disconnect.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# --- message types: node -> hub ---
MSG_HELLO = "hello"
MSG_DESIRED_REQUEST = "desired_request"
MSG_PROPOSAL_SUBMIT = "proposal_submit"
MSG_REPORT_SUBMIT = "report_submit"
MSG_PING = "ping"

# --- message types: hub -> node ---
MSG_HELLO_ACK = "hello_ack"
MSG_DESIRED_PUSH = "desired_push"
MSG_ACK = "ack"
MSG_ERROR = "error"
MSG_PONG = "pong"
MSG_SHUTDOWN = "shutdown"

NODE_TO_HUB_TYPES = frozenset({MSG_HELLO, MSG_DESIRED_REQUEST, MSG_PROPOSAL_SUBMIT, MSG_REPORT_SUBMIT, MSG_PING})
HUB_TO_NODE_TYPES = frozenset({MSG_HELLO_ACK, MSG_DESIRED_PUSH, MSG_ACK, MSG_ERROR, MSG_PONG, MSG_SHUTDOWN})

# --- error codes (stable ASCII for programs; `message` is Chinese for humans) ---
ERR_UNAUTHORIZED = "unauthorized"
ERR_FORBIDDEN = "forbidden"
ERR_NOT_FOUND = "not_found"
ERR_BAD_ENVELOPE = "bad_envelope"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_PAYLOAD_INVALID = "payload_invalid"
ERR_SERVICE_ERROR = "service_error"
ERR_REVOKED = "revoked"
ERR_TOO_MANY_CONNECTIONS = "too_many_connections"
ERR_VERSION_MISMATCH = "version_mismatch"
ERR_INTERNAL = "internal"

# --- close codes (RFC 6455 private range) ---
CLOSE_NORMAL = 1000
CLOSE_INTERNAL_ERROR = 1011
CLOSE_SHUTTING_DOWN = 4000
CLOSE_REVOKED = 4003
CLOSE_NODE_REMOVED = 4004
CLOSE_VERSION_MISMATCH = 4008
CLOSE_TOO_MANY_CONNECTIONS = 4009
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_HANDSHAKE_TIMEOUT = 4408

# Closes the agent must not retry against with the exponential ramp: a stale
# token reconnecting every second is an argon2 DoS on the hub.
AUTH_FATAL_CLOSE_CODES = frozenset(
    {
        CLOSE_REVOKED,
        CLOSE_NODE_REMOVED,
        CLOSE_VERSION_MISMATCH,
        CLOSE_UNAUTHORIZED,
        CLOSE_FORBIDDEN,
        CLOSE_NOT_FOUND,
    }
)


class EnvelopeError(ValueError):
    """Raised by `decode` for anything that isn't a well-formed envelope."""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Envelope:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    re: str | None = None
    v: int = PROTOCOL_VERSION
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "type": self.type,
            "id": self.id,
            "re": self.re,
            "ts": self.ts,
            "payload": dict(self.payload),
        }


def encode(envelope: Envelope) -> str:
    return json.dumps(envelope.to_dict(), ensure_ascii=False)


def decode(raw: str) -> Envelope:
    """Parse a frame. Raises `EnvelopeError` with a human-readable reason.

    Version mismatch is NOT decided here — callers need to distinguish it from a
    malformed frame so they can pick error code / close code. Check `env.v`.
    """
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise EnvelopeError(f"消息超过 {MAX_FRAME_BYTES} 字节上限")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise EnvelopeError(f"不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError("信封顶层必须是对象")

    version = data.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise EnvelopeError("缺少或非法的 v 字段")

    msg_type = data.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise EnvelopeError("缺少或非法的 type 字段")

    msg_id = data.get("id")
    if not isinstance(msg_id, str) or not msg_id:
        raise EnvelopeError("缺少或非法的 id 字段")

    re_id = data.get("re")
    if re_id is not None and not isinstance(re_id, str):
        raise EnvelopeError("re 字段必须是字符串或 null")

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise EnvelopeError("payload 必须是对象")

    ts = data.get("ts")
    if ts is not None and not isinstance(ts, str):
        raise EnvelopeError("ts 字段必须是字符串")

    return Envelope(
        type=msg_type,
        payload=payload,
        id=msg_id,
        re=re_id,
        v=version,
        ts=ts if isinstance(ts, str) else _now_iso(),
    )


def make_error(code: str, message: str, *, re: str | None = None) -> Envelope:
    return Envelope(type=MSG_ERROR, payload={"code": code, "message": message}, re=re)


def make_ack(payload: dict[str, Any], *, re: str) -> Envelope:
    return Envelope(type=MSG_ACK, payload=payload, re=re)
