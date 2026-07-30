"""Resident spoke-side agent: one long-lived WebSocket to the hub.

Protocol: `docs/architecture/sync_ws_protocol.md`. Started by
`dn42ctl node agent` / `dn42ctl-node-agent.service`; replaces the old
`dn42ctl-node-once.timer` polling loop entirely.

Pure asyncio (the `websockets` client is asyncio-only), unlike the hub side which
uses anyio because it lives inside Starlette.

`run_agent` takes injectable `connect_factory` / `sleep` / `rng`. Those are not
decoration: they are what makes the reconnect loop testable with zero real
sleeps and a deterministic jitter sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from dn42ctl import __version__
from dn42ctl.node_client import build_auth_headers, build_ws_url
from dn42ctl.node_config import AgentOptions, NodeConfig, NodeConfigError, load_node_config
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.node_agent import read_cache, store_desired
from dn42ctl.services.node_apply import apply, apply_summary
from dn42ctl.ws_protocol import (
    AUTH_FATAL_CLOSE_CODES,
    MAX_FRAME_BYTES,
    MSG_ACK,
    MSG_DESIRED_PUSH,
    MSG_DESIRED_REQUEST,
    MSG_ERROR,
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_PING,
    MSG_PONG,
    MSG_REPORT_SUBMIT,
    MSG_SHUTDOWN,
    Envelope,
    EnvelopeError,
    decode,
    encode,
)

Emit = Callable[[str], None]
Sleep = Callable[[float], Awaitable[None]]
ConnectFactory = Callable[..., Any]


class _Reconnect(Exception):  # noqa: N818 — control flow, not an error condition
    """Unwind the current session and go back to the reconnect loop."""

    def __init__(self, reason: str, *, close_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.close_code = close_code


def _default_emit(message: str) -> None:
    # systemd captures stdout into the journal, so a plain print is the log.
    print(message, flush=True)  # noqa: T201


def _default_connect_factory(url: str, **kwargs: Any) -> Any:
    from websockets.asyncio.client import connect

    return connect(url, **kwargs)


class _Session:
    """One connected lifetime: hello, then reader ∥ heartbeat ∥ reconcile."""

    def __init__(
        self,
        *,
        ws: Any,
        node_config: NodeConfig,
        settings: AgentOptions,
        emit: Emit,
        sleep: Sleep,
    ) -> None:
        self._ws = ws
        self._cfg = node_config
        self._settings = settings
        self._emit = emit
        self._sleep = sleep
        # Serializes rendering: a reconcile answer and a pushed update must never
        # write /etc/bird concurrently.
        self._apply_lock = asyncio.Lock()

    async def _send(self, envelope: Envelope) -> None:
        await self._ws.send(encode(envelope))

    async def run(self) -> None:
        await self._send(
            Envelope(
                type=MSG_HELLO,
                payload={
                    "node_id": self._cfg.node_id,
                    "agent_version": __version__,
                    "cached_revision": self._cached_revision(),
                },
            )
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._heartbeat_loop())
            tg.create_task(self._reconcile_loop())
            await self._reader_loop()
            raise _Reconnect("连接已关闭")

    def _cached_revision(self) -> str | None:
        cached = read_cache(node_config=self._cfg)
        return None if cached is None else cached.revision

    async def _reader_loop(self) -> None:
        while True:
            try:
                raw = await self._ws.recv()
            except Exception as exc:  # noqa: BLE001 — any transport failure means reconnect
                raise _Reconnect(f"读取失败: {exc}") from exc
            if isinstance(raw, bytes | bytearray):
                raw = raw.decode("utf-8", errors="replace")
            await self._dispatch(raw)

    async def _dispatch(self, raw: str) -> None:
        try:
            env = decode(raw)
        except EnvelopeError as exc:
            self._emit(f"警告: 收到非法信封: {exc}")
            return

        if env.type == MSG_DESIRED_PUSH:
            await self._handle_desired_push(env)
        elif env.type == MSG_HELLO_ACK:
            self._emit(
                f"已连接 server={self._cfg.server} revision={env.payload.get('revision')} "
                f"in_sync={env.payload.get('in_sync')}"
            )
        elif env.type == MSG_ERROR:
            self._emit(f"server 返回错误 [{env.payload.get('code')}]: {env.payload.get('message')}")
        elif env.type == MSG_SHUTDOWN:
            raise _Reconnect(f"server 正在关闭: {env.payload.get('reason')}")
        elif env.type in (MSG_ACK, MSG_PONG):
            pass
        else:
            self._emit(f"警告: 未知消息类型 {env.type}")

    async def _handle_desired_push(self, env: Envelope) -> None:
        desired = env.payload.get("desired")
        if not isinstance(desired, dict):
            await self._report_error("desired_push 缺少 desired 对象")
            return
        try:
            async with self._apply_lock:
                await asyncio.to_thread(store_desired, node_config=self._cfg, payload=desired)
                result = await asyncio.to_thread(apply, node_config=self._cfg)
        except (Dn42CtlError, OSError) as exc:
            # One bad push must never take the connection down with it.
            self._emit(f"错误: apply 失败: {exc}")
            await self._report_error(str(exc))
            return

        self._emit(apply_summary(result))
        await self._send(
            Envelope(
                type=MSG_REPORT_SUBMIT,
                payload={
                    "kind": "apply_result",
                    # Same shape as `dn42ctl node once` reports, so hub-side
                    # consumers need not care which transport delivered it.
                    "payload": {
                        "ok": True,
                        "revision": result.revision,
                        "create": sum(1 for d in result.diffs if d.action == "create"),
                        "update": sum(1 for d in result.diffs if d.action == "update"),
                        "unchanged": sum(1 for d in result.diffs if d.action == "unchanged"),
                        "delete": sum(1 for d in result.diffs if d.action == "delete"),
                    },
                },
            )
        )

    async def _report_error(self, message: str) -> None:
        with contextlib.suppress(Exception):  # noqa: BLE001 — reporting is best-effort
            await self._send(
                Envelope(type=MSG_REPORT_SUBMIT, payload={"kind": "error", "payload": {"message": message}})
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._sleep(self._settings.heartbeat_interval_seconds)
            await self._send(Envelope(type=MSG_PING))

    async def _reconcile_loop(self) -> None:
        """Periodic full re-check. With no fallback timer this is the only
        self-heal for a drift the push path somehow missed.
        """
        while True:
            await self._sleep(self._settings.reconcile_interval_seconds)
            await self._send(Envelope(type=MSG_DESIRED_REQUEST, payload={"reason": "reconcile"}))


def _apply_from_cache(node_config: NodeConfig, emit: Emit) -> None:
    """Render from the local cache before the first connection attempt.

    Deleting `node-once.timer` also deleted its `OnBootSec`, so without this a
    spoke that reboots while the hub is unreachable would never write /etc/bird
    at all. This is what makes "no fallback timer" a safe design, not an
    optimization.
    """
    if read_cache(node_config=node_config) is None:
        return
    try:
        result = apply(node_config=node_config)
    except (Dn42CtlError, OSError) as exc:
        emit(f"警告: 启动时从缓存 apply 失败: {exc}")
        return
    emit(f"启动时从本地缓存 apply: {apply_summary(result)}")


async def run_agent(
    *,
    node_config_path: Path,
    settings: AgentOptions | None = None,
    connect_factory: ConnectFactory | None = None,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    emit: Emit = _default_emit,
) -> None:
    """Connect, sync, and keep reconnecting until cancelled.

    `node_config_path` rather than a loaded `NodeConfig`: the file is re-read on
    every reconnect, so a rotated token recovers without `systemctl restart`.
    """
    connect = connect_factory or _default_connect_factory
    rng = rng or random.Random()  # noqa: S311 — jitter, not cryptography
    attempt = 0
    bootstrapped = False

    while True:
        try:
            cfg = load_node_config(node_config_path)
        except NodeConfigError as exc:
            emit(f"错误: {exc}")
            raise

        effective = settings or cfg.agent

        if not bootstrapped:
            bootstrapped = True
            await asyncio.to_thread(_apply_from_cache, cfg, emit)

        close_code: int | None = None
        try:
            async with connect(
                build_ws_url(server=cfg.server, node_id=cfg.node_id),
                additional_headers=build_auth_headers(token=cfg.token),
                max_size=MAX_FRAME_BYTES,
            ) as ws:
                attempt = 0
                try:
                    await _Session(ws=ws, node_config=cfg, settings=effective, emit=emit, sleep=sleep).run()
                finally:
                    # Read inside the `async with`, where `ws` is in scope; a
                    # failure to connect at all leaves this None (normal backoff).
                    close_code = getattr(ws, "close_code", None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure funnels into backoff
            emit(f"连接结束: {_describe(exc)}")

        delay, attempt = _next_delay(effective, attempt=attempt, close_code=close_code, rng=rng)
        if close_code in AUTH_FATAL_CLOSE_CODES:
            emit(f"鉴权被拒 (close={close_code})，{delay:.0f}s 后重试；请检查 node.toml 中的 token")
        await sleep(delay)


def _describe(exc: BaseException) -> str:
    """Unwrap TaskGroup's ExceptionGroup down to something worth logging."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, _Reconnect):
        return exc.reason
    return f"{type(exc).__name__}: {exc}"


def _next_delay(
    settings: AgentOptions,
    *,
    attempt: int,
    close_code: int | None,
    rng: random.Random,
) -> tuple[float, int]:
    """Full jitter: `uniform(0, min(cap, base * 2**attempt))`.

    Full rather than equal jitter because the dominant failure mode is the whole
    fleet reconnecting in lockstep after a hub restart, and `authenticate` is an
    O(nodes) argon2 scan — the flatter the burst, the better.

    Auth-fatal closes bypass the ramp entirely and use a long fixed wait.
    """
    if close_code in AUTH_FATAL_CLOSE_CODES:
        return settings.auth_retry_seconds, 0
    ceiling = min(settings.reconnect_max_seconds, settings.reconnect_initial_seconds * (2**attempt))
    return rng.uniform(0, ceiling), attempt + 1  # noqa: S311 — jitter, not cryptography
