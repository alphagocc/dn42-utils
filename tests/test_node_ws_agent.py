"""Resident node agent: reconnect, heartbeat, reconcile, apply.

`run_agent` is `async def`, so `TestClient` can't drive it — but a plain
`asyncio.run()` inside a sync test works with no async plugin, *provided* the
module exposes seams. `connect_factory`, `sleep` and `rng` are exactly that:
a scripted fake socket, a sleep that records the delay and returns immediately
(raising a sentinel after N calls to break the infinite loop), and a seeded RNG
so the jitter is deterministic. There is not one real sleep in this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from pathlib import Path
from typing import Any

import pytest

import dn42ctl.node_ws_agent as agent_mod
from dn42ctl.node_config import AgentOptions, NodeConfig, save_node_config
from dn42ctl.node_ws_agent import _next_delay, run_agent
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.ws_protocol import (
    CLOSE_REVOKED,
    CLOSE_SHUTTING_DOWN,
    CLOSE_UNAUTHORIZED,
    MSG_DESIRED_PUSH,
    MSG_DESIRED_REQUEST,
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_PING,
    MSG_REPORT_SUBMIT,
    Envelope,
    encode,
)

NODE_A = "11111111-1111-4111-8111-111111111111"


def _rng(seed: int = 0) -> random.Random:
    """Seeded RNG so the full-jitter delays are reproducible in assertions."""
    return random.Random(seed)  # noqa: S311 — deterministic test jitter, not cryptography


class _Stop(Exception):
    """Sentinel that unwinds the agent's infinite reconnect loop."""


class _FakeWS:
    """Scripted peer. `inbound` frames are handed to the agent in order; once
    exhausted, `recv` raises to simulate the connection dropping.
    """

    def __init__(self, inbound: list[str], *, close_code: int | None = None) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []
        self.close_code = close_code

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def recv(self) -> str:
        if not self._inbound:
            raise ConnectionResetError("peer closed")
        return self._inbound.pop(0)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def sent_types(self) -> list[str]:
        return [m["type"] for m in self.sent]


def _connect_factory(sockets: list[_FakeWS], *, calls: list[dict[str, Any]] | None = None):
    """Hand out one prepared socket per connection attempt."""

    def factory(url: str, **kwargs: Any) -> _FakeWS:
        if calls is not None:
            calls.append({"url": url, **kwargs})
        if not sockets:
            raise ConnectionRefusedError("no more sockets")
        return sockets.pop(0)

    return factory


def _sleeper(limit: int, *, park_at: float = 10.0):
    """Records delays, never actually sleeps, raises `_Stop` past `limit`.

    Sleeps at or above `park_at` block forever instead of being counted. That is
    how "the heartbeat/reconcile timer has not fired yet" is modelled: with the
    default 60s/900s intervals those loops would otherwise spin instantly and
    exhaust the budget before the reader ever sees a frame.
    """
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds >= park_at:
            await asyncio.Event().wait()  # never set; cancelled with the task group
        delays.append(seconds)
        if len(delays) > limit:
            raise _Stop
        await asyncio.sleep(0)

    return sleep, delays


def _run(coro) -> None:
    async def main() -> None:
        with contextlib.suppress(_Stop):
            await coro

    asyncio.run(main())


@pytest.fixture
def node_toml(tmp_path: Path) -> Path:
    path = tmp_path / "node.toml"
    save_node_config(
        path,
        NodeConfig(
            server="https://hub.example",
            node_id=NODE_A,
            token="tok",
            cache_db_path=tmp_path / "cache.sqlite3",
        ),
    )
    return path


@pytest.fixture(autouse=True)
def _no_real_apply(monkeypatch: pytest.MonkeyPatch):
    """Default: apply is a no-op. Tests that care override it."""
    monkeypatch.setattr(agent_mod, "read_cache", lambda **_kw: None)
    monkeypatch.setattr(agent_mod, "store_desired", lambda **_kw: None)
    monkeypatch.setattr(agent_mod, "apply", lambda **_kw: _FakeApplyResult())
    monkeypatch.setattr(agent_mod, "apply_summary", lambda _r: "applied")
    yield


class _FakeDiff:
    def __init__(self, action: str) -> None:
        self.action = action


class _FakeApplyResult:
    revision = "rev-1"
    diffs = (_FakeDiff("create"), _FakeDiff("unchanged"))


def _desired_push(revision: str = "rev-1") -> str:
    return encode(
        Envelope(
            type=MSG_DESIRED_PUSH,
            payload={
                "revision": revision,
                "generated_at": "2026-07-29T00:00:00+00:00",
                "desired": {"node_id": NODE_A, "revision": revision, "bgp_peers": [], "ibgp_peers": []},
            },
        )
    )


class TestHandshake:
    def test_sends_hello_first(self, node_toml: Path) -> None:
        ws = _FakeWS([])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert ws.sent_types()[0] == MSG_HELLO
        hello = ws.sent[0]["payload"]
        assert hello["node_id"] == NODE_A
        assert hello["agent_version"]
        assert hello["cached_revision"] is None

    def test_reports_cached_revision(self, node_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Cached:
            revision = "rev-cached"

        monkeypatch.setattr(agent_mod, "read_cache", lambda **_kw: _Cached())
        ws = _FakeWS([])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert ws.sent[0]["payload"]["cached_revision"] == "rev-cached"

    def test_connect_url_and_auth(self, node_toml: Path) -> None:
        calls: list[dict[str, Any]] = []
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([_FakeWS([])], calls=calls),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert calls[0]["url"] == f"wss://hub.example/api/v1/nodes/{NODE_A}/ws"
        assert calls[0]["additional_headers"] == {"Authorization": "Bearer tok"}


class TestDesiredPush:
    def test_applies_and_reports(self, node_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stored: list[dict] = []
        applied: list[int] = []
        monkeypatch.setattr(agent_mod, "store_desired", lambda **kw: stored.append(kw["payload"]))
        monkeypatch.setattr(agent_mod, "apply", lambda **_kw: (applied.append(1), _FakeApplyResult())[1])

        ws = _FakeWS([encode(Envelope(type=MSG_HELLO_ACK, payload={"in_sync": False})), _desired_push()])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert len(stored) == 1
        assert stored[0]["revision"] == "rev-1"
        assert applied == [1]

        reports = [m for m in ws.sent if m["type"] == MSG_REPORT_SUBMIT]
        assert len(reports) == 1
        payload = reports[0]["payload"]
        assert payload["kind"] == "apply_result"
        # Same shape `dn42ctl node once` reports, so the hub can't tell them apart.
        assert payload["payload"] == {
            "ok": True,
            "revision": "rev-1",
            "create": 1,
            "update": 0,
            "unchanged": 1,
            "delete": 0,
        }

    def test_malformed_push_reports_error_and_survives(self, node_toml: Path) -> None:
        ws = _FakeWS(
            [
                encode(Envelope(type=MSG_DESIRED_PUSH, payload={"revision": "r"})),  # no `desired`
                _desired_push(),
            ]
        )
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        kinds = [m["payload"]["kind"] for m in ws.sent if m["type"] == MSG_REPORT_SUBMIT]
        # Error reported, then the session kept going and handled the good push.
        assert kinds == ["error", "apply_result"]

    def test_apply_failure_reports_error_and_survives(self, node_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def flaky(**_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Dn42CtlError("渲染失败")
            return _FakeApplyResult()

        monkeypatch.setattr(agent_mod, "apply", flaky)
        ws = _FakeWS([_desired_push("r1"), _desired_push("r2")])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        kinds = [m["payload"]["kind"] for m in ws.sent if m["type"] == MSG_REPORT_SUBMIT]
        assert kinds == ["error", "apply_result"]

    def test_garbage_frame_is_ignored(self, node_toml: Path) -> None:
        ws = _FakeWS(["}}} not json", _desired_push()])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert any(m["type"] == MSG_REPORT_SUBMIT for m in ws.sent)


class TestHeartbeatAndReconcile:
    def test_emits_ping_and_reconcile_on_injected_clock(self, node_toml: Path) -> None:
        """Both loops are driven purely by the injected sleep, so a few ticks are
        enough to prove they fire — no wall-clock waiting.
        """

        class _NeverEndingWS(_FakeWS):
            async def recv(self) -> str:
                # Yield forever so the heartbeat/reconcile tasks get to run.
                await asyncio.sleep(0)
                return encode(Envelope(type=MSG_HELLO_ACK, payload={"in_sync": True}))

        ws = _NeverEndingWS([])
        sleep, _ = _sleeper(6)
        _run(
            run_agent(
                node_config_path=node_toml,
                settings=AgentOptions(heartbeat_interval_seconds=0.0001, reconcile_interval_seconds=0.0002),
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert MSG_PING in ws.sent_types()
        assert MSG_DESIRED_REQUEST in ws.sent_types()
        reconciles = [m for m in ws.sent if m["type"] == MSG_DESIRED_REQUEST]
        assert reconciles[0]["payload"]["reason"] == "reconcile"


class TestStartupApply:
    def test_applies_from_cache_before_connecting(self, node_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deleting node-once.timer removed OnBootSec; this replaces it. A spoke
        that reboots while the hub is down must still render /etc/bird.
        """
        order: list[str] = []

        class _Cached:
            revision = "rev-cached"

        monkeypatch.setattr(agent_mod, "read_cache", lambda **_kw: _Cached())
        monkeypatch.setattr(agent_mod, "apply", lambda **_kw: (order.append("apply"), _FakeApplyResult())[1])

        def factory(url: str, **_kw: Any):
            order.append("connect")
            raise ConnectionRefusedError("hub down")

        sleep, _ = _sleeper(1)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=factory,
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert order[:2] == ["apply", "connect"]

    def test_no_cache_means_no_apply(self, node_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        applied: list[int] = []
        monkeypatch.setattr(agent_mod, "read_cache", lambda **_kw: None)
        monkeypatch.setattr(agent_mod, "apply", lambda **_kw: (applied.append(1), _FakeApplyResult())[1])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([_FakeWS([])]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert applied == []

    def test_startup_apply_failure_is_not_fatal(self, node_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Cached:
            revision = "r"

        monkeypatch.setattr(agent_mod, "read_cache", lambda **_kw: _Cached())

        def boom(**_kw):
            raise Dn42CtlError("no permission")

        monkeypatch.setattr(agent_mod, "apply", boom)
        ws = _FakeWS([])
        sleep, _ = _sleeper(0)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=_connect_factory([ws]),
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        # Still went on to connect.
        assert ws.sent_types()[0] == MSG_HELLO


class TestReconnect:
    def test_rereads_node_toml_every_attempt(self, node_toml: Path) -> None:
        """Token rotation must recover without `systemctl restart`."""
        calls: list[dict[str, Any]] = []

        def factory(url: str, **kwargs: Any):
            calls.append(kwargs)
            if len(calls) == 1:
                # Rotate the token on disk between attempts.
                save_node_config(
                    node_toml,
                    NodeConfig(
                        server="https://hub.example",
                        node_id=NODE_A,
                        token="rotated",
                        cache_db_path=node_toml.parent / "cache.sqlite3",
                    ),
                )
            raise ConnectionRefusedError("down")

        sleep, _ = _sleeper(2)
        _run(
            run_agent(
                node_config_path=node_toml,
                connect_factory=factory,
                sleep=sleep,
                rng=_rng(0),
                emit=lambda _m: None,
            )
        )
        assert calls[0]["additional_headers"]["Authorization"] == "Bearer tok"
        assert calls[1]["additional_headers"]["Authorization"] == "Bearer rotated"

    def test_backoff_delays_stay_within_the_ramp(self, node_toml: Path) -> None:
        settings = AgentOptions(reconnect_initial_seconds=1.0, reconnect_max_seconds=8.0)

        def factory(url: str, **_kw: Any):
            raise ConnectionRefusedError("down")

        sleep, delays = _sleeper(6)
        _run(
            run_agent(
                node_config_path=node_toml,
                settings=settings,
                connect_factory=factory,
                sleep=sleep,
                rng=_rng(1234),
                emit=lambda _m: None,
            )
        )
        for i, delay in enumerate(delays):
            ceiling = min(8.0, 1.0 * (2**i))
            assert 0.0 <= delay <= ceiling, f"attempt {i}: {delay} exceeds {ceiling}"
        assert all(d <= 8.0 for d in delays)

    def test_successful_connection_resets_the_ramp(self, node_toml: Path) -> None:
        settings = AgentOptions(reconnect_initial_seconds=4.0, reconnect_max_seconds=64.0)
        sockets = [_FakeWS([]), _FakeWS([])]
        sleep, delays = _sleeper(2)
        _run(
            run_agent(
                node_config_path=node_toml,
                settings=settings,
                connect_factory=_connect_factory(sockets),
                sleep=sleep,
                rng=_rng(7),
                emit=lambda _m: None,
            )
        )
        # Each attempt connected, so attempt counter resets: ceiling stays at base.
        assert all(d <= 4.0 for d in delays[:2])


class TestNextDelay:
    def test_full_jitter_bounds(self) -> None:
        settings = AgentOptions(reconnect_initial_seconds=1.0, reconnect_max_seconds=60.0)
        rng = _rng(0)
        for attempt in range(10):
            delay, nxt = _next_delay(settings, attempt=attempt, close_code=None, rng=rng)
            assert 0.0 <= delay <= min(60.0, 2**attempt)
            assert nxt == attempt + 1

    def test_caps_at_max(self) -> None:
        settings = AgentOptions(reconnect_initial_seconds=1.0, reconnect_max_seconds=5.0)
        rng = _rng(0)
        for _ in range(50):
            delay, _ = _next_delay(settings, attempt=20, close_code=None, rng=rng)
            assert delay <= 5.0

    @pytest.mark.parametrize("code", [CLOSE_UNAUTHORIZED, CLOSE_REVOKED])
    def test_auth_fatal_uses_fixed_long_wait(self, code: int) -> None:
        """A stale token retrying every second would be an argon2 DoS on the hub."""
        settings = AgentOptions(reconnect_max_seconds=60.0, auth_retry_seconds=300.0)
        delay, attempt = _next_delay(settings, attempt=9, close_code=code, rng=_rng(0))
        assert delay == 300.0
        assert attempt == 0

    def test_hub_shutdown_uses_the_normal_ramp(self) -> None:
        """A restarting hub is transient — it must not trigger the 5-minute wait."""
        settings = AgentOptions(reconnect_initial_seconds=1.0, reconnect_max_seconds=60.0, auth_retry_seconds=300.0)
        delay, _ = _next_delay(settings, attempt=0, close_code=CLOSE_SHUTTING_DOWN, rng=_rng(0))
        assert delay <= 1.0


class TestCancellation:
    def test_cancel_propagates(self, node_toml: Path) -> None:
        """systemctl stop must not be swallowed by the retry loop."""

        async def main() -> None:
            task = asyncio.create_task(
                run_agent(
                    node_config_path=node_toml,
                    connect_factory=_connect_factory([]),
                    sleep=asyncio.sleep,
                    rng=_rng(0),
                    emit=lambda _m: None,
                )
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
