from __future__ import annotations

import json

import pytest

from dn42ctl.ws_protocol import (
    AUTH_FATAL_CLOSE_CODES,
    CLOSE_FORBIDDEN,
    CLOSE_NODE_REMOVED,
    CLOSE_NOT_FOUND,
    CLOSE_REVOKED,
    CLOSE_SHUTTING_DOWN,
    CLOSE_TOO_MANY_CONNECTIONS,
    CLOSE_UNAUTHORIZED,
    CLOSE_VERSION_MISMATCH,
    HUB_TO_NODE_TYPES,
    MAX_FRAME_BYTES,
    MSG_ACK,
    MSG_ERROR,
    MSG_HELLO,
    NODE_TO_HUB_TYPES,
    PROTOCOL_VERSION,
    Envelope,
    EnvelopeError,
    decode,
    encode,
    make_ack,
    make_error,
    new_id,
)


class TestRoundTrip:
    def test_basic(self) -> None:
        env = Envelope(type=MSG_HELLO, payload={"node_id": "n1", "cached_revision": None})
        back = decode(encode(env))
        assert back.type == env.type
        assert back.id == env.id
        assert back.payload == env.payload
        assert back.v == PROTOCOL_VERSION
        assert back.re is None

    def test_re_correlation_survives(self) -> None:
        request = Envelope(type=MSG_HELLO)
        reply = make_ack({"proposal_id": 3}, re=request.id)
        assert decode(encode(reply)).re == request.id

    def test_non_ascii_not_escaped(self) -> None:
        """Error messages are Chinese; escaping them would bloat every frame."""
        raw = encode(make_error("service_error", "节点不存在"))
        assert "节点不存在" in raw

    def test_ids_are_unique(self) -> None:
        assert len({new_id() for _ in range(100)}) == 100

    def test_payload_is_copied(self) -> None:
        payload = {"a": 1}
        env = Envelope(type=MSG_HELLO, payload=payload)
        env.to_dict()["payload"]["a"] = 999
        assert payload == {"a": 1}


class TestDecodeRejects:
    def test_not_json(self) -> None:
        with pytest.raises(EnvelopeError, match="JSON"):
            decode("{not json")

    def test_top_level_array(self) -> None:
        with pytest.raises(EnvelopeError, match="对象"):
            decode("[]")

    def test_missing_version(self) -> None:
        with pytest.raises(EnvelopeError, match="v 字段"):
            decode(json.dumps({"type": "ping", "id": "a", "payload": {}}))

    def test_bool_version_rejected(self) -> None:
        """bool is an int subclass in Python — must not sneak through."""
        with pytest.raises(EnvelopeError, match="v 字段"):
            decode(json.dumps({"v": True, "type": "ping", "id": "a", "payload": {}}))

    def test_missing_type(self) -> None:
        with pytest.raises(EnvelopeError, match="type 字段"):
            decode(json.dumps({"v": 1, "id": "a", "payload": {}}))

    def test_empty_type(self) -> None:
        with pytest.raises(EnvelopeError, match="type 字段"):
            decode(json.dumps({"v": 1, "type": "", "id": "a", "payload": {}}))

    def test_missing_id(self) -> None:
        with pytest.raises(EnvelopeError, match="id 字段"):
            decode(json.dumps({"v": 1, "type": "ping", "payload": {}}))

    def test_non_string_re(self) -> None:
        with pytest.raises(EnvelopeError, match="re 字段"):
            decode(json.dumps({"v": 1, "type": "ack", "id": "a", "re": 7, "payload": {}}))

    def test_non_object_payload(self) -> None:
        with pytest.raises(EnvelopeError, match="payload"):
            decode(json.dumps({"v": 1, "type": "ping", "id": "a", "payload": []}))

    def test_oversized_frame(self) -> None:
        blob = "x" * (MAX_FRAME_BYTES + 1)
        with pytest.raises(EnvelopeError, match="上限"):
            decode(blob)

    def test_payload_defaults_to_empty(self) -> None:
        env = decode(json.dumps({"v": 1, "type": "ping", "id": "a"}))
        assert env.payload == {}

    def test_missing_ts_is_filled_in(self) -> None:
        """ts is informational; a peer omitting it must not break the frame."""
        assert decode(json.dumps({"v": 1, "type": "ping", "id": "a"})).ts


class TestVersionIsCallerDecided:
    def test_mismatched_version_decodes(self) -> None:
        """decode must NOT reject on version — callers distinguish a version skew
        (error{version_mismatch} + close 4008) from a malformed frame.
        """
        env = decode(json.dumps({"v": 99, "type": "ping", "id": "a", "payload": {}}))
        assert env.v == 99
        assert env.v != PROTOCOL_VERSION


class TestConstants:
    def test_protocol_version_is_one(self) -> None:
        assert PROTOCOL_VERSION == 1

    def test_direction_sets_are_disjoint(self) -> None:
        assert not (NODE_TO_HUB_TYPES & HUB_TO_NODE_TYPES)

    def test_close_codes_in_private_range(self) -> None:
        for code in (
            CLOSE_SHUTTING_DOWN,
            CLOSE_REVOKED,
            CLOSE_NODE_REMOVED,
            CLOSE_VERSION_MISMATCH,
            CLOSE_TOO_MANY_CONNECTIONS,
            CLOSE_UNAUTHORIZED,
            CLOSE_FORBIDDEN,
            CLOSE_NOT_FOUND,
        ):
            assert 4000 <= code <= 4999

    def test_auth_fatal_set(self) -> None:
        """These must use the long fixed retry, not the exponential ramp."""
        assert CLOSE_UNAUTHORIZED in AUTH_FATAL_CLOSE_CODES
        assert CLOSE_REVOKED in AUTH_FATAL_CLOSE_CODES
        assert CLOSE_NODE_REMOVED in AUTH_FATAL_CLOSE_CODES
        # Hub restarting is transient — must NOT trigger the 5-minute backoff.
        assert CLOSE_SHUTTING_DOWN not in AUTH_FATAL_CLOSE_CODES
        assert CLOSE_TOO_MANY_CONNECTIONS not in AUTH_FATAL_CLOSE_CODES

    def test_frame_ceiling_below_uvicorn_default(self) -> None:
        assert MAX_FRAME_BYTES < 16 * 1024 * 1024


class TestHelpers:
    def test_make_error(self) -> None:
        env = make_error("bad_envelope", "坏消息", re="req-1")
        assert env.type == MSG_ERROR
        assert env.payload == {"code": "bad_envelope", "message": "坏消息"}
        assert env.re == "req-1"

    def test_make_error_without_re(self) -> None:
        assert make_error("internal", "boom").re is None

    def test_make_ack(self) -> None:
        env = make_ack({"report_id": 9}, re="req-2")
        assert env.type == MSG_ACK
        assert env.payload == {"report_id": 9}
        assert env.re == "req-2"
