"""Hub side of the node WebSocket sync channel.

See `docs/architecture/sync_ws_protocol.md`. The FastAPI endpoint itself lives in
`api.py` and delegates here; everything this module needs is passed in explicitly,
so there is no import cycle back to `api`.

Two moving parts:

  * `ConnectionRegistry` — in-process map of live node connections. Correct only
    because `dn42ctl serve` runs a single uvicorn worker.
  * `run_sync_watcher` — polls `sync_events` (written by *other processes*, e.g.
    `dn42ctl bgp peer add`) and turns rows into pushes or disconnects.

Every service call in here is synchronous and opens its own sqlite connection, so
each one is dispatched to a worker thread. Running them on the event loop would
stall every other connection — a desired-state build alone touches sqlite repeatedly.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.to_thread import run_sync as _run_in_thread

from dn42ctl.constants import SYNC_EVENT_ACCESS_REVOKED, SYNC_EVENT_DESIRED
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore, SyncEventStore
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.desired_state import (
    build_desired_state,
    compute_desired_fingerprint,
    require_managed_node_exists,
)
from dn42ctl.services.proposals import submit_proposal
from dn42ctl.services.reports import submit_report
from dn42ctl.ws_protocol import (
    CLOSE_HANDSHAKE_TIMEOUT,
    CLOSE_NOT_FOUND,
    CLOSE_REVOKED,
    CLOSE_SHUTTING_DOWN,
    CLOSE_TOO_MANY_CONNECTIONS,
    CLOSE_UNAUTHORIZED,
    CLOSE_VERSION_MISMATCH,
    ERR_BAD_ENVELOPE,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PAYLOAD_INVALID,
    ERR_REVOKED,
    ERR_SERVICE_ERROR,
    ERR_TOO_MANY_CONNECTIONS,
    ERR_UNAUTHORIZED,
    ERR_UNKNOWN_TYPE,
    ERR_VERSION_MISMATCH,
    MSG_DESIRED_PUSH,
    MSG_DESIRED_REQUEST,
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_PING,
    MSG_PONG,
    MSG_PROPOSAL_SUBMIT,
    MSG_REPORT_SUBMIT,
    MSG_SHUTDOWN,
    PROTOCOL_VERSION,
    Envelope,
    EnvelopeError,
    decode,
    encode,
    make_ack,
    make_error,
    new_id,
)

if TYPE_CHECKING:
    from dn42ctl.config import AppConfig

logger = logging.getLogger("dn42ctl.ws_hub")

# A half-dead socket must not evict a healthy new one, so several connections per
# node are allowed; beyond this the oldest is closed, which is what stops a
# reconnect storm from leaking file descriptors.
MAX_CONNECTIONS_PER_NODE = 4

# Heartbeats are per-minute per node; without this the last_seen write would be
# amplified across every connection a node holds.
LAST_SEEN_THROTTLE_SECONDS = 60.0

HELLO_TIMEOUT_SECONDS = 15.0
DEFAULT_SYNC_POLL_INTERVAL = 1.0
_WATCHER_BATCH = 500


async def _to_thread(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Run a blocking, kwargs-only service function off the event loop.

    anyio's `run_sync` is positional-only, hence the partial.
    """
    return await _run_in_thread(functools.partial(fn, **kwargs))


@dataclass
class NodeConnection:
    """One live WebSocket, plus the little state the push path needs."""

    node_id: str
    conn_id: str
    websocket: Any  # starlette.websockets.WebSocket
    send_lock: anyio.Lock = field(default_factory=anyio.Lock)
    # buffer=1 + send_nowait gives free coalescing: many events while a push is in
    # flight collapse into one follow-up check. anyio.Event can't be cleared, so a
    # stream is the right primitive here.
    wake_tx: MemoryObjectSendStream[None] | None = None
    wake_rx: MemoryObjectReceiveStream[None] | None = None
    # Content digest of the last desired state we actually sent on THIS socket.
    # The comparison against it is what makes repeated wakeups idempotent.
    last_pushed_hash: str | None = None
    last_seen_touched_at: float = 0.0
    agent_version: str = ""

    async def send(self, envelope: Envelope) -> None:
        async with self.send_lock:
            await self.websocket.send_text(encode(envelope))


class ConnectionRegistry:
    """node_id -> conn_id -> NodeConnection, guarded by one lock."""

    def __init__(self) -> None:
        self._by_node: dict[str, dict[str, NodeConnection]] = {}
        self._lock = anyio.Lock()

    async def register(self, conn: NodeConnection) -> None:
        async with self._lock:
            conns = self._by_node.setdefault(conn.node_id, {})
            conns[conn.conn_id] = conn
            surplus = list(conns.values())[:-MAX_CONNECTIONS_PER_NODE]
        for old in surplus:
            await self._close_one(old, CLOSE_TOO_MANY_CONNECTIONS, ERR_TOO_MANY_CONNECTIONS, "连接数超限")

    async def unregister(self, conn: NodeConnection) -> None:
        async with self._lock:
            conns = self._by_node.get(conn.node_id)
            if conns is None:
                return
            conns.pop(conn.conn_id, None)
            if not conns:
                self._by_node.pop(conn.node_id, None)

    async def connections_for(self, node_id: str) -> list[NodeConnection]:
        async with self._lock:
            return list(self._by_node.get(node_id, {}).values())

    async def all_connections(self) -> list[NodeConnection]:
        async with self._lock:
            return [c for conns in self._by_node.values() for c in conns.values()]

    async def is_connected(self, node_id: str) -> bool:
        async with self._lock:
            return bool(self._by_node.get(node_id))

    async def notify(self, node_id: str) -> None:
        """Nudge every connection of `node_id` to re-check its desired state.

        Non-blocking and coalescing: a full buffer means a check is already
        pending, which covers this event too.
        """
        for conn in await self.connections_for(node_id):
            if conn.wake_tx is None:
                continue
            with contextlib.suppress(anyio.WouldBlock, anyio.BrokenResourceError, anyio.ClosedResourceError):
                conn.wake_tx.send_nowait(None)

    async def close_node(self, node_id: str, code: int, *, err_code: str, message: str) -> None:
        for conn in await self.connections_for(node_id):
            await self._close_one(conn, code, err_code, message)

    async def close_all(self, code: int = CLOSE_SHUTTING_DOWN) -> None:
        for conn in await self.all_connections():
            with contextlib.suppress(Exception):  # noqa: BLE001 — shutdown is best-effort
                await conn.send(Envelope(type=MSG_SHUTDOWN, payload={"reason": "server shutting down"}))
                await conn.websocket.close(code=code)

    async def _close_one(self, conn: NodeConnection, code: int, err_code: str, message: str) -> None:
        # The peer may already be gone; closing a dead socket must not propagate.
        with contextlib.suppress(Exception):  # noqa: BLE001
            await conn.send(make_error(err_code, message))
        with contextlib.suppress(Exception):  # noqa: BLE001
            await conn.websocket.close(code=code)


@dataclass(frozen=True)
class _AuthResult:
    ok: bool
    node_id: str = ""
    close_code: int = CLOSE_UNAUTHORIZED
    err_code: str = ERR_UNAUTHORIZED
    message: str = ""


def _extract_bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _authenticate_sync(*, db_path: Path, token: str, node_id: str) -> _AuthResult:
    """Runs in a worker thread. One token check per connection, never per frame."""
    db = Database.open(db_path)
    try:
        node = ManagedNodeStore(db.connection).authenticate(token)
    finally:
        db.close()
    if node is None:
        return _AuthResult(ok=False, message="token 无效")
    if node.node_id != node_id:
        from dn42ctl.ws_protocol import CLOSE_FORBIDDEN, ERR_FORBIDDEN

        return _AuthResult(
            ok=False,
            close_code=CLOSE_FORBIDDEN,
            err_code=ERR_FORBIDDEN,
            message="node_id 与 token 不匹配",
        )
    return _AuthResult(ok=True, node_id=node.node_id)


async def serve_node_connection(
    websocket: Any,
    *,
    node_id: str,
    db_path: Path,
    config: AppConfig | None,
    registry: ConnectionRegistry,
) -> None:
    """Full server side of one connection: handshake, hello, then steady state.

    ASGI turns a pre-`accept()` close into an HTTP 403, which gives the client an
    opaque `InvalidStatus` with no reason. So we accept first and only then
    authenticate, closing with a specific code (and an `error` frame) on failure.
    """
    await websocket.accept()

    token = _extract_bearer(websocket.headers.get("authorization"))
    if token is None:
        await _reject(websocket, CLOSE_UNAUTHORIZED, ERR_UNAUTHORIZED, "缺少 Bearer token")
        return

    auth = await _to_thread(_authenticate_sync, db_path=db_path, token=token, node_id=node_id)
    if not auth.ok:
        await _reject(websocket, auth.close_code, auth.err_code, auth.message)
        return

    try:
        await _to_thread(require_managed_node_exists, db_path=db_path, node_id=node_id)
    except Dn42CtlError as exc:
        await _reject(websocket, CLOSE_NOT_FOUND, ERR_NOT_FOUND, str(exc))
        return

    tx, rx = anyio.create_memory_object_stream[None](max_buffer_size=1)
    conn = NodeConnection(
        node_id=node_id,
        conn_id=new_id(),
        websocket=websocket,
        wake_tx=tx,
        wake_rx=rx,
    )

    hello = await _await_hello(conn)
    if hello is None:
        return
    conn.agent_version = str(hello.payload.get("agent_version") or "")
    cached_revision = hello.payload.get("cached_revision")

    await registry.register(conn)
    try:
        in_sync = await _send_hello_ack(conn, db_path=db_path, cached_revision=cached_revision, re=hello.id)
        if not in_sync:
            await _maybe_push_desired(conn, db_path=db_path, force=True)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_pusher_loop, conn, db_path)
            await _reader_loop(conn, db_path=db_path, config=config)
            tg.cancel_scope.cancel()
    finally:
        await registry.unregister(conn)
        with contextlib.suppress(Exception):  # noqa: BLE001
            tx.close()
            rx.close()


async def _reject(websocket: Any, code: int, err_code: str, message: str) -> None:
    with contextlib.suppress(Exception):  # noqa: BLE001 — peer may already be gone
        await websocket.send_text(encode(make_error(err_code, message)))
    with contextlib.suppress(Exception):  # noqa: BLE001
        await websocket.close(code=code)


async def _await_hello(conn: NodeConnection) -> Envelope | None:
    """Read exactly one `hello`, bounded so an unauthenticated peer can't squat."""
    try:
        with anyio.fail_after(HELLO_TIMEOUT_SECONDS):
            raw = await conn.websocket.receive_text()
    except TimeoutError:
        await _reject(conn.websocket, CLOSE_HANDSHAKE_TIMEOUT, ERR_BAD_ENVELOPE, "等待 hello 超时")
        return None
    except Exception:  # noqa: BLE001 — disconnect during handshake
        return None

    try:
        env = decode(raw)
    except EnvelopeError as exc:
        await _reject(conn.websocket, CLOSE_HANDSHAKE_TIMEOUT, ERR_BAD_ENVELOPE, str(exc))
        return None
    if env.v != PROTOCOL_VERSION:
        await _reject(
            conn.websocket,
            CLOSE_VERSION_MISMATCH,
            ERR_VERSION_MISMATCH,
            f"协议版本不匹配: 收到 v={env.v}, 期望 v={PROTOCOL_VERSION}",
        )
        return None
    if env.type != MSG_HELLO:
        await _reject(
            conn.websocket, CLOSE_HANDSHAKE_TIMEOUT, ERR_UNKNOWN_TYPE, f"首条消息必须是 hello, 收到 {env.type}"
        )
        return None
    return env


async def _send_hello_ack(conn: NodeConnection, *, db_path: Path, cached_revision: Any, re: str) -> bool:
    """Reply to hello. Returns whether the node's cache is already current."""
    from dn42ctl import __version__

    fingerprint = await _to_thread(compute_desired_fingerprint, db_path=db_path, node_id=conn.node_id)
    revision = await _current_revision(db_path=db_path, node_id=conn.node_id)
    in_sync = isinstance(cached_revision, str) and cached_revision == revision
    if in_sync:
        # Cache matches, so the node already holds this content: record the hash
        # now, otherwise the first wakeup would push a state it already has.
        conn.last_pushed_hash = fingerprint.content_hash
    await conn.send(
        Envelope(
            type=MSG_HELLO_ACK,
            payload={
                "node_id": conn.node_id,
                "server_version": __version__,
                "revision": revision,
                "in_sync": in_sync,
            },
            re=re,
        )
    )
    return in_sync


async def _current_revision(*, db_path: Path, node_id: str) -> str | None:
    def _read() -> str | None:
        from dn42ctl.db_managed import RevisionStore

        db = Database.open(db_path)
        try:
            store = RevisionStore(db.connection)
            pin = store.get_pin(node_id)
            if pin is not None:
                return pin.revision
            return store.latest_revision(node_id)
        finally:
            db.close()

    return await _run_in_thread(_read)


async def _maybe_push_desired(
    conn: NodeConnection, *, db_path: Path, force: bool = False, re: str | None = None
) -> None:
    """Push desired state, unless its content is what we last sent on this socket.

    The fingerprint is read-only, so the common "nothing changed" path costs one
    query and zero writes. `force` is for explicit `desired_request`, which must
    always answer.
    """
    fingerprint = await _to_thread(compute_desired_fingerprint, db_path=db_path, node_id=conn.node_id)
    if not force and fingerprint.content_hash == conn.last_pushed_hash:
        return
    state = await _to_thread(build_desired_state, db_path=db_path, node_id=conn.node_id)
    await conn.send(
        Envelope(
            type=MSG_DESIRED_PUSH,
            payload={
                "revision": state.revision,
                "generated_at": state.generated_at,
                "desired": state.to_dict(),
            },
            re=re,
        )
    )
    conn.last_pushed_hash = fingerprint.content_hash


async def _pusher_loop(conn: NodeConnection, db_path: Path) -> None:
    if conn.wake_rx is None:
        return
    async for _ in conn.wake_rx:
        try:
            await _maybe_push_desired(conn, db_path=db_path)
        except Exception:  # noqa: BLE001 — one bad push must not drop the connection
            logger.exception("推送 desired state 失败: node=%s", conn.node_id)


async def _reader_loop(conn: NodeConnection, *, db_path: Path, config: AppConfig | None) -> None:
    while True:
        try:
            raw = await conn.websocket.receive_text()
        except Exception:  # noqa: BLE001 — normal disconnect
            return
        try:
            await _dispatch(conn, raw, db_path=db_path, config=config)
        except Exception:  # noqa: BLE001 — a bad frame must not kill the connection
            logger.exception("处理消息失败: node=%s", conn.node_id)
            with contextlib.suppress(Exception):  # noqa: BLE001
                await conn.send(make_error(ERR_INTERNAL, "服务端内部错误"))


async def _dispatch(conn: NodeConnection, raw: str, *, db_path: Path, config: AppConfig | None) -> None:
    try:
        env = decode(raw)
    except EnvelopeError as exc:
        await conn.send(make_error(ERR_BAD_ENVELOPE, str(exc)))
        return
    if env.v != PROTOCOL_VERSION:
        await conn.send(make_error(ERR_VERSION_MISMATCH, f"协议版本不匹配: v={env.v}", re=env.id))
        return

    if env.type == MSG_PING:
        await conn.send(Envelope(type=MSG_PONG, re=env.id))
        await _touch_last_seen(conn, db_path=db_path)
        return

    if env.type == MSG_DESIRED_REQUEST:
        await _maybe_push_desired(conn, db_path=db_path, force=True, re=env.id)
        return

    if env.type == MSG_PROPOSAL_SUBMIT:
        await _handle_proposal(conn, env, db_path=db_path, config=config)
        return

    if env.type == MSG_REPORT_SUBMIT:
        await _handle_report(conn, env, db_path=db_path)
        return

    # Unknown type is not fatal: the connection stays open.
    await conn.send(make_error(ERR_UNKNOWN_TYPE, f"未知消息类型: {env.type}", re=env.id))


async def _handle_proposal(conn: NodeConnection, env: Envelope, *, db_path: Path, config: AppConfig | None) -> None:
    kind = env.payload.get("kind")
    payload = env.payload.get("payload")
    source = env.payload.get("source", "push")
    if not isinstance(kind, str) or not isinstance(payload, dict) or not isinstance(source, str):
        await conn.send(make_error(ERR_PAYLOAD_INVALID, "proposal_submit 需要 kind/payload/source", re=env.id))
        return
    try:
        proposal = await _to_thread(
            submit_proposal,
            db_path=db_path,
            node_id=conn.node_id,
            source=source,
            kind=kind,
            payload=payload,
            config=config,
        )
    except Dn42CtlError as exc:
        await conn.send(make_error(ERR_SERVICE_ERROR, str(exc), re=env.id))
        return
    await conn.send(make_ack({"proposal_id": proposal.id, "status": proposal.status}, re=env.id))
    await _touch_last_seen(conn, db_path=db_path)


async def _handle_report(conn: NodeConnection, env: Envelope, *, db_path: Path) -> None:
    kind = env.payload.get("kind")
    payload = env.payload.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict):
        await conn.send(make_error(ERR_PAYLOAD_INVALID, "report_submit 需要 kind/payload", re=env.id))
        return
    try:
        report = await _to_thread(
            submit_report,
            db_path=db_path,
            node_id=conn.node_id,
            kind=kind,
            payload=payload,
        )
    except Dn42CtlError as exc:
        await conn.send(make_error(ERR_SERVICE_ERROR, str(exc), re=env.id))
        return
    await conn.send(make_ack({"report_id": report.id, "received_at": report.received_at}, re=env.id))
    await _touch_last_seen(conn, db_path=db_path)


async def _touch_last_seen(conn: NodeConnection, *, db_path: Path) -> None:
    now = time.monotonic()
    if now - conn.last_seen_touched_at < LAST_SEEN_THROTTLE_SECONDS:
        return
    conn.last_seen_touched_at = now

    def _write() -> None:
        db = Database.open(db_path)
        try:
            ManagedNodeStore(db.connection).touch_last_seen(conn.node_id)
        finally:
            db.close()

    with contextlib.suppress(Exception):  # noqa: BLE001 — liveness bookkeeping is best-effort
        await _run_in_thread(_write)


async def run_sync_watcher(
    registry: ConnectionRegistry,
    db_path_getter: Callable[[], Path | None],
    poll_interval_getter: Callable[[], float] | None = None,
    *,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Turn `sync_events` rows into pushes and disconnects. Runs until cancelled.

    The cursor is anchored at MAX(id) **before** signalling started, so callers can
    `await tg.start(...)` and know that everything written from then on will be
    seen. Anchoring lazily on the first tick instead would leave a poll-interval
    hole at startup during which changes are silently skipped.

    Starting at MAX(id) rather than 0 is safe because a wakeup only triggers a
    *re-check* of current content, never a replay of a diff: anything older is
    already covered by each connection's initial read. That also means trimming
    the table can never strand the cursor.
    """
    last_id: int | None = None
    db_path = db_path_getter()
    if db_path is not None:
        try:
            last_id = await _to_thread(_read_latest_id, db_path=db_path)
        except Exception:  # noqa: BLE001 — anchor lazily instead of failing startup
            logger.exception("sync_events 游标初始化失败, 稍后重试")
    task_status.started()

    while True:
        interval = poll_interval_getter() if poll_interval_getter else DEFAULT_SYNC_POLL_INTERVAL
        await sleep(interval)
        try:
            db_path = db_path_getter()
            if db_path is None:  # app not configured yet — idle rather than crash
                continue
            if last_id is None:
                last_id = await _to_thread(_read_latest_id, db_path=db_path)
                continue
            events = await _to_thread(_fetch_events, db_path=db_path, last_id=last_id)
            if not events:
                continue
            last_id = max(e.id for e in events)
            revoked = {e.node_id for e in events if e.kind == SYNC_EVENT_ACCESS_REVOKED}
            changed = {e.node_id for e in events if e.kind == SYNC_EVENT_DESIRED}
            for node_id in revoked:
                await registry.close_node(
                    node_id, CLOSE_REVOKED, err_code=ERR_REVOKED, message="访问已被撤销 (token 轮换或节点删除)"
                )
            for node_id in changed - revoked:
                await registry.notify(node_id)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:  # noqa: BLE001 — a transient DB error must not kill fleet sync
            logger.exception("sync_events watcher 本轮失败, 继续下一轮")


def _read_latest_id(*, db_path: Path) -> int:
    db = Database.open(db_path)
    try:
        return SyncEventStore(db.connection).latest_id()
    finally:
        db.close()


def _fetch_events(*, db_path: Path, last_id: int) -> list[Any]:
    db = Database.open(db_path)
    try:
        return SyncEventStore(db.connection).fetch_since(last_id, limit=_WATCHER_BATCH)
    finally:
        db.close()
