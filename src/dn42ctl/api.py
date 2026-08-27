from __future__ import annotations

import contextlib
import secrets as _secrets
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

from dn42ctl.config import AppConfig
from dn42ctl.constants import UNSET
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services import (
    Dn42CtlError,
    accept_proposal,
    add_node,
    build_desired_state,
    clear_rollback,
    create_bgp_peer,
    create_ibgp_peer,
    delete_bgp_peer,
    delete_ibgp_peer,
    genconf,
    get_node,
    get_node_status,
    get_pinned,
    import_report,
    list_nodes,
    list_proposals,
    list_reports,
    list_revisions,
    modify_bgp_peer,
    modify_ibgp_peer,
    reject_proposal,
    remove_node,
    require_managed_node_exists,
    rollback_to,
    rotate_token,
    set_policy,
    submit_proposal,
    submit_report,
)
from dn42ctl.services.auto_peer import (
    AutoPeerError,
    AutoPeerExpiredError,
    AutoPeerSessionError,
    start_challenge,
    start_lookup,
    submit_peer,
    verify_challenge,
)
from dn42ctl.services.db_browse import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    browse_table,
    list_tables,
    table_page_to_dict,
)
from dn42ctl.services.node_address import set_node_addresses
from dn42ctl.services.show import show_bgp_peers, show_ibgp_peers, show_wg_tunnels
from dn42ctl.validators import (
    validate_allowed_ips_list,
    validate_asn,
    validate_babel_type,
    validate_endpoint,
    validate_endpoint_host,
    validate_ipv6_address,
    validate_listen_port,
    validate_pubkey,
    validate_router_id,
    validate_rxcost,
)
from dn42ctl.ws_hub import (
    DEFAULT_SYNC_POLL_INTERVAL,
    ConnectionRegistry,
    run_sync_watcher,
    serve_node_connection,
)

_bearer = HTTPBearer(auto_error=False)

_config: AppConfig | None = None
_db_path: Path | None = None
_admin_token: str = ""
_sync_poll_interval: float = DEFAULT_SYNC_POLL_INTERVAL

# In-process map of live node WebSockets. Correct only because `dn42ctl serve`
# runs a single uvicorn worker; see docs/architecture/sync_ws_protocol.md.
_registry = ConnectionRegistry()


def configure(
    *,
    config: AppConfig,
    db_path: Path,
    token: str,
    sync_poll_interval: float = DEFAULT_SYNC_POLL_INTERVAL,
) -> None:
    """Inject runtime config. `token` becomes the admin token.

    Node tokens are managed separately via `dn42ctl node token rotate` and stored
    as SHA-256 hashes in `managed_nodes.api_token_hash`; they are not configured here.

    `sync_poll_interval` is how often the watcher scans `sync_events`, i.e. the
    worst-case delay between a config change and the node being told about it.
    """
    global _config, _db_path, _admin_token, _sync_poll_interval
    _config = config
    _db_path = db_path
    _admin_token = token
    _sync_poll_interval = sync_poll_interval


@dataclass(frozen=True)
class Principal:
    kind: Literal["admin", "node"]
    node_id: str | None  # None for admin


def _get_config() -> AppConfig:
    if _config is None:
        raise HTTPException(status_code=500, detail="Server not configured")
    return _config


def _get_db_path() -> Path:
    if _db_path is None:
        raise HTTPException(status_code=500, detail="Server not configured")
    return _db_path


def _db_path_or_none() -> Path | None:
    """Non-raising variant for the background watcher, which has no request to fail."""
    return _db_path


def _self_node_id() -> str | None:
    """hub 自身在 managed_nodes 里的 node_id（is_self=1）。"""
    db = Database.open(_get_db_path())
    try:
        node = ManagedNodeStore(db.connection).get_self()
    finally:
        db.close()
    return node.node_id if node is not None else None


def _resolve_target_node(node_id: str | None) -> str:
    """返回该 admin 请求应当操作的节点 id。

    显式传入的 `?node_id=` 优先，但必须是已注册的受管节点；未传时取 hub 的 self
    节点（`managed_nodes.is_self=1`），而不是 `config.node_id`。

    这两个 id 的来源相互独立：`config.toml` 里的 `node_id` 由 `dn42ctl init` 写入，
    self 节点 id 则由 `serve_bootstrap` 生成并保存在 `/var/lib/dn42ctl/self_node_id`，
    两者之间从不互相校验。早先 admin API 按前者写入 peer，而 desired-state 按后者读取
    peer，一旦两个 id 分叉，管理员在 UI 中添加的 peer 就永远不会下发，并且不会有任何
    报错。默认对齐到 self 节点即可消除这条静默失败路径。

    只有在 self 行不存在时（`--no-self-register` 部署）才回退到 `config.node_id`。
    该回退值**不做存在性校验**——这类部署本来就可能没有对应的 `managed_nodes` 行。
    """
    if node_id:
        require_managed_node_exists(db_path=_get_db_path(), node_id=node_id)
        return node_id
    return _self_node_id() or _get_config().node_id


def _resolve_principal(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Parse the Bearer token into a Principal (admin or node).

    Order:
      1. If matches admin token (constant-time compare), return admin.
      2. Else look up against managed_nodes.api_token_hash; if match, return node.
      3. Else 401.
    """
    if cred is None or not cred.credentials:
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = cred.credentials
    if _admin_token and _secrets.compare_digest(token, _admin_token):
        return Principal(kind="admin", node_id=None)
    db_path = _get_db_path()
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        node = store.authenticate(token)
    finally:
        db.close()
    if node is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return Principal(kind="node", node_id=node.node_id)


def require_admin(
    principal: Annotated[Principal, Depends(_resolve_principal)],
) -> Principal:
    if principal.kind != "admin":
        raise HTTPException(status_code=403, detail="Admin token required")
    return principal


def require_node_self_or_admin(
    node_id: str,
    principal: Annotated[Principal, Depends(_resolve_principal)],
) -> Principal:
    """Allow admins to act on any node; node tokens must match the path node_id."""
    if principal.kind == "admin":
        return principal
    if principal.kind == "node" and principal.node_id == node_id:
        return principal
    raise HTTPException(status_code=403, detail="Forbidden")


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the sync_events watcher for the lifetime of the server.

    Existing tests construct `TestClient(app)` without a context manager, so
    lifespan never runs there and no watcher is spawned. WebSocket tests use
    `with TestClient(app)` and get the real thing.
    """
    async with anyio.create_task_group() as tg:
        # `start`, not `start_soon`: startup blocks until the watcher has anchored
        # its sync_events cursor, so no change can slip through the gap between
        # the server accepting connections and the first poll.
        await tg.start(run_sync_watcher, _registry, _db_path_or_none, lambda: _sync_poll_interval)
        try:
            yield
        finally:
            await _registry.close_all()
            tg.cancel_scope.cancel()


app = FastAPI(title="dn42ctl API", lifespan=_lifespan)
admin_prefix = "/api/admin"
node_prefix = "/api/v1/nodes"

_admin_nodes_router = APIRouter(prefix=admin_prefix, dependencies=[Depends(require_admin)])

_node_router = APIRouter(prefix=node_prefix)

# Public (no-auth) routes for the auto-peer wizard.
public_prefix = "/api/public"
_public_router = APIRouter(prefix=public_prefix)


# --- Admin: node management ---


class NodeAddRequest(BaseModel):
    node_id: str
    name: str


class NodePolicyPatchRequest(BaseModel):
    peer_add: str | None = None
    peer_modify: str | None = None
    peer_delete: str | None = None
    report: str | None = None


class NodePatchRequest(BaseModel):
    """局部更新节点。

    **字段缺席 = 不改动；显式传 `null` = 清除（交还节点本地管理）。** 这个区分只能靠
    `model_fields_set` 表达，不能简化成 `is not None` 判断 —— 那样就永远无法清除一个字段。
    """

    name: str | None = None
    enabled: bool | None = None
    endpoint_host: str | None = None
    own_ipv6: str | None = None
    router_id: str | None = None
    propagate: bool = True
    dry_run: bool = False

    @field_validator("endpoint_host")
    @classmethod
    def _check_endpoint_host(cls, v: str | None) -> str | None:
        return None if v is None else validate_endpoint_host(v)

    @field_validator("own_ipv6")
    @classmethod
    def _check_own_ipv6(cls, v: str | None) -> str | None:
        return None if v is None else validate_ipv6_address(v, field_name="own_ipv6")

    @field_validator("router_id")
    @classmethod
    def _check_router_id(cls, v: str | None) -> str | None:
        return None if v is None else validate_router_id(v)


def _managed_node_to_dict(node) -> dict:  # noqa: ANN001 — ManagedNode dataclass
    return {
        "node_id": node.node_id,
        "name": node.name,
        "write_policy": node.write_policy,
        "enabled": node.enabled,
        "is_self": node.is_self,
        "last_seen_at": node.last_seen_at,
        "created_at": node.created_at,
        "updated_at": node.updated_at,
        "has_token": node.api_token_hash is not None,
        "endpoint_host": node.endpoint_host,
        "own_ipv6": node.own_ipv6,
        "router_id": node.router_id,
    }


@_admin_nodes_router.get("/nodes")
def api_list_managed_nodes() -> list[dict]:
    try:
        nodes = list_nodes(db_path=_get_db_path())
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_managed_node_to_dict(n) for n in nodes]


@_admin_nodes_router.post("/nodes", status_code=201)
def api_add_managed_node(body: NodeAddRequest) -> dict:
    try:
        node = add_node(db_path=_get_db_path(), node_id=body.node_id, name=body.name)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _managed_node_to_dict(node)


@_admin_nodes_router.get("/nodes/{node_id}")
def api_get_managed_node(node_id: str) -> dict:
    try:
        node = get_node(db_path=_get_db_path(), node_id=node_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _managed_node_to_dict(node)


@_admin_nodes_router.patch("/nodes/{node_id}")
def api_patch_managed_node(node_id: str, body: NodePatchRequest) -> dict:
    # 用 model_fields_set 而非 `is not None`:body 里没出现的字段保持不变,显式传 null
    # 才是"清除该字段"。两者语义不同,合并掉就再也无法取消中心管理了。
    given = body.model_fields_set
    try:
        result = set_node_addresses(
            db_path=_get_db_path(),
            node_id=node_id,
            name=body.name if "name" in given and body.name is not None else UNSET,
            enabled=body.enabled if "enabled" in given and body.enabled is not None else UNSET,
            endpoint_host=body.endpoint_host if "endpoint_host" in given else UNSET,
            own_ipv6=body.own_ipv6 if "own_ipv6" in given else UNSET,
            router_id=body.router_id if "router_id" in given else UNSET,
            propagate=body.propagate,
            dry_run=body.dry_run,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **_managed_node_to_dict(result.node),
        "propagated": [asdict(c) for c in result.changes],
        "warnings": result.warnings,
        "dry_run": result.dry_run,
    }


@_admin_nodes_router.delete("/nodes/{node_id}")
def api_remove_managed_node(node_id: str, force: Annotated[bool, Query()] = False) -> dict:
    try:
        removed = remove_node(db_path=_get_db_path(), node_id=node_id, force=force)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **_managed_node_to_dict(removed.node),
        "self_node_toml_error": removed.self_node_toml_error,
    }


@_admin_nodes_router.post("/nodes/{node_id}/token")
def api_rotate_node_token(node_id: str) -> dict:
    try:
        rotated = rotate_token(db_path=_get_db_path(), node_id=node_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # node.toml 改写失败不影响 200:hash 已经换掉,明文只在这个响应里出现一次。
    # 但必须让调用方看见,否则 hub 自己的 agent 会被静默锁在门外。
    return {
        "node_id": rotated.node_id,
        "token": rotated.plaintext,
        "self_node_toml_updated": rotated.self_node_toml_updated,
        "self_node_toml_error": rotated.self_node_toml_error,
    }


@_admin_nodes_router.patch("/nodes/{node_id}/policy")
def api_patch_node_policy(node_id: str, body: NodePolicyPatchRequest) -> dict:
    try:
        node = set_policy(
            db_path=_get_db_path(),
            node_id=node_id,
            peer_add=body.peer_add,
            peer_modify=body.peer_modify,
            peer_delete=body.peer_delete,
            report=body.report,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _managed_node_to_dict(node)


# --- Node-token routes (/api/v1/nodes/{node_id}/...) ---


class ProposalSubmitRequest(BaseModel):
    source: str = "push"
    kind: str
    payload: dict


class ReportSubmitRequest(BaseModel):
    kind: str
    payload: dict


def _proposal_to_dict(p) -> dict:  # noqa: ANN001
    return {
        "id": p.id,
        "node_id": p.node_id,
        "source": p.source,
        "kind": p.kind,
        "payload": p.payload,
        "status": p.status,
        "received_at": p.received_at,
        "decided_at": p.decided_at,
        "message": p.message,
    }


def _report_to_dict(r) -> dict:  # noqa: ANN001
    return {
        "id": r.id,
        "node_id": r.node_id,
        "kind": r.kind,
        "payload": r.payload,
        "received_at": r.received_at,
        "imported_at": r.imported_at,
    }


@_node_router.get("/{node_id}/desired")
def api_node_desired(
    node_id: str,
    _: Annotated[Principal, Depends(require_node_self_or_admin)],
) -> dict:
    try:
        require_managed_node_exists(db_path=_get_db_path(), node_id=node_id)
        state = build_desired_state(db_path=_get_db_path(), node_id=node_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Best-effort: a failed last_seen write must not fail the pull.
    try:
        db = Database.open(_get_db_path())
        try:
            ManagedNodeStore(db.connection).touch_last_seen(node_id)
        finally:
            db.close()
    except Exception:  # noqa: BLE001, S110 — touch_last_seen is best-effort
        pass
    return state.to_dict()


@_node_router.post("/{node_id}/proposals", status_code=201)
def api_node_post_proposal(
    node_id: str,
    body: ProposalSubmitRequest,
    _: Annotated[Principal, Depends(require_node_self_or_admin)],
) -> dict:
    try:
        proposal = submit_proposal(
            db_path=_get_db_path(),
            node_id=node_id,
            source=body.source,
            kind=body.kind,
            payload=body.payload,
            config=_get_config(),
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _proposal_to_dict(proposal)


@_node_router.post("/{node_id}/reports", status_code=201)
def api_node_post_report(
    node_id: str,
    body: ReportSubmitRequest,
    _: Annotated[Principal, Depends(require_node_self_or_admin)],
) -> dict:
    try:
        report = submit_report(
            db_path=_get_db_path(),
            node_id=node_id,
            kind=body.kind,
            payload=body.payload,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _report_to_dict(report)


@_node_router.websocket("/{node_id}/ws")
async def api_node_ws(websocket: WebSocket, node_id: str) -> None:
    """Resident node agent channel. Protocol: docs/architecture/sync_ws_protocol.md.

    Deliberately NOT using `Depends(_resolve_principal)`: it works by duck-typing
    (HTTPBearer accepts the WebSocket), but signals failure with HTTPException,
    which Starlette cannot render on a WebSocket. `serve_node_connection` parses
    the header itself and closes with a specific code instead.
    """
    await serve_node_connection(
        websocket,
        node_id=node_id,
        db_path=_get_db_path(),
        config=_config,
        registry=_registry,
    )


@_node_router.get("/{node_id}/status")
def api_node_status(
    node_id: str,
    _: Annotated[Principal, Depends(require_node_self_or_admin)],
) -> dict:
    """Central-side view of the node: last_seen, current revision, pinned revision."""
    try:
        status = get_node_status(db_path=_get_db_path(), node_id=node_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "node_id": status.node_id,
        "name": status.name,
        "enabled": status.enabled,
        "is_self": status.is_self,
        "has_token": status.has_token,
        "last_seen_at": status.last_seen_at,
        "current_revision": status.current_revision,
        "pinned_revision": status.pinned_revision,
    }


# --- Admin: proposals / reports listing ---


@_admin_nodes_router.get("/nodes/{node_id}/proposals")
def api_list_proposals(
    node_id: str,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[dict]:
    try:
        rows = list_proposals(db_path=_get_db_path(), node_id=node_id, status=status, limit=limit)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_proposal_to_dict(p) for p in rows]


@_admin_nodes_router.get("/nodes/{node_id}/reports")
def api_list_reports(
    node_id: str,
    kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[dict]:
    try:
        rows = list_reports(db_path=_get_db_path(), node_id=node_id, kind=kind, limit=limit)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_report_to_dict(r) for r in rows]


# --- Admin: proposal decisions / report import ---


class ProposalRejectRequest(BaseModel):
    reason: str


@_admin_nodes_router.post("/proposals/{proposal_id}/accept")
def api_accept_proposal(proposal_id: int) -> dict:
    try:
        p = accept_proposal(config=_get_config(), db_path=_get_db_path(), proposal_id=proposal_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _proposal_to_dict(p)


@_admin_nodes_router.post("/proposals/{proposal_id}/reject")
def api_reject_proposal(proposal_id: int, body: ProposalRejectRequest) -> dict:
    try:
        p = reject_proposal(db_path=_get_db_path(), proposal_id=proposal_id, reason=body.reason)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _proposal_to_dict(p)


@_admin_nodes_router.post("/reports/{report_id}/import")
def api_import_report(report_id: int) -> dict:
    try:
        counts = import_report(config=_get_config(), db_path=_get_db_path(), report_id=report_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"report_id": report_id, **counts}


# --- Admin: revisions / rollback (stage 5) ---


class NodeRollbackRequest(BaseModel):
    revision: str


def _revision_to_dict(rev) -> dict:  # noqa: ANN001
    return {
        "id": rev.id,
        "node_id": rev.node_id,
        "revision": rev.revision,
        "generated_at": rev.generated_at,
    }


@_admin_nodes_router.get("/nodes/{node_id}/revisions")
def api_list_revisions(
    node_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict:
    try:
        rows = list_revisions(db_path=_get_db_path(), node_id=node_id, limit=limit)
        pin = get_pinned(db_path=_get_db_path(), node_id=node_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "pinned_revision": pin.revision if pin else None,
        "revisions": [_revision_to_dict(r) for r in rows],
    }


@_admin_nodes_router.post("/nodes/{node_id}/rollback")
def api_rollback(node_id: str, body: NodeRollbackRequest) -> dict:
    try:
        rev = rollback_to(db_path=_get_db_path(), node_id=node_id, revision=body.revision)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"pinned": _revision_to_dict(rev)}


@_admin_nodes_router.delete("/nodes/{node_id}/rollback")
def api_clear_rollback(node_id: str) -> dict:
    try:
        clear_rollback(db_path=_get_db_path(), node_id=node_id)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"node_id": node_id, "pinned": None}


# --- Admin: BGP peer CRUD ---


class BgpPeerModifyRequest(BaseModel):
    peer_public_key: str
    endpoint: str = ""
    peer_lla: str
    listen_port: int | None = None
    allowed_ips: list[str] | None = None

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str) -> str:
        return validate_pubkey(v)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return validate_endpoint(v, allow_empty=True)

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="Peer LLA")

    @field_validator("listen_port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v

    @field_validator("allowed_ips")
    @classmethod
    def _check_allowed_ips(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            return validate_allowed_ips_list(v)
        return v


class BgpPeerCreateRequest(BgpPeerModifyRequest):
    peer_asn: int

    @field_validator("peer_asn")
    @classmethod
    def _check_asn(cls, v: int) -> int:
        return validate_asn(v)


@_admin_nodes_router.get("/bgp/peers")
def api_list_bgp_peers(live: bool = Query(False), node_id: str | None = Query(None)) -> list[dict]:
    config = _get_config()
    db_path = _get_db_path()
    try:
        peers = show_bgp_peers(config=config, db_path=db_path, include_live=live, node_id=_resolve_target_node(node_id))
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(p) for p in peers]


@_admin_nodes_router.post("/bgp/peers", status_code=201)
def api_create_bgp_peer(body: BgpPeerCreateRequest, node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = create_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=body.peer_asn,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend="networkd",
            listen_port=body.listen_port,
            node_id=_resolve_target_node(node_id),
            render_files=False,
            allowed_ips=body.allowed_ips,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


@_admin_nodes_router.put("/bgp/peers/{peer_asn}")
def api_modify_bgp_peer(peer_asn: int, body: BgpPeerModifyRequest, node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = modify_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=peer_asn,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend="networkd",
            listen_port=body.listen_port,
            node_id=_resolve_target_node(node_id),
            render_files=False,
            allowed_ips=body.allowed_ips,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


@_admin_nodes_router.delete("/bgp/peers/{peer_asn}")
def api_delete_bgp_peer(peer_asn: int, node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = delete_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=peer_asn,
            node_id=_resolve_target_node(node_id),
            render_files=False,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


# --- Admin: iBGP peer CRUD ---


class _IbgpPeerValidators(BaseModel):
    """Shared fields + validators for iBGP create/modify request models."""

    peer_ip: str
    babel_rxcost: int = 20
    babel_type: str = "tunnel"
    listen_port: int | None = None
    allowed_ips: list[str] | None = None
    # 这条记录所代表的受管节点,用于地址传播。modify 时字段缺席 = 保留既有关联。
    remote_node_id: str | None = None

    @field_validator("peer_ip")
    @classmethod
    def _check_peer_ip(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="Peer IP")

    @field_validator("babel_rxcost")
    @classmethod
    def _check_rxcost(cls, v: int) -> int:
        return validate_rxcost(v)

    @field_validator("babel_type")
    @classmethod
    def _check_babel_type(cls, v: str) -> str:
        return validate_babel_type(v)

    @field_validator("listen_port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v

    @field_validator("allowed_ips")
    @classmethod
    def _check_allowed_ips(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            return validate_allowed_ips_list(v)
        return v


class IbgpPeerCreateRequest(_IbgpPeerValidators):
    name: str
    has_wg: bool = True
    peer_public_key: str | None = None
    endpoint: str | None = None
    peer_lla: str | None = None

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_pubkey(v)
        return v

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_endpoint(v, allow_empty=True)
        return v

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_ipv6_address(v, field_name="Peer LLA")
        return v


class IbgpPeerModifyRequest(_IbgpPeerValidators):
    peer_public_key: str
    endpoint: str = ""
    peer_lla: str

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str) -> str:
        return validate_pubkey(v)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return validate_endpoint(v, allow_empty=True)

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="Peer LLA")


@_admin_nodes_router.get("/ibgp/peers")
def api_list_ibgp_peers(live: bool = Query(False), node_id: str | None = Query(None)) -> list[dict]:
    config = _get_config()
    db_path = _get_db_path()
    try:
        peers = show_ibgp_peers(
            config=config, db_path=db_path, include_live=live, node_id=_resolve_target_node(node_id)
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(p) for p in peers]


@_admin_nodes_router.post("/ibgp/peers", status_code=201)
def api_create_ibgp_peer(body: IbgpPeerCreateRequest, node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = create_ibgp_peer(
            config=config,
            db_path=db_path,
            name=body.name,
            peer_ip=body.peer_ip,
            has_wg=body.has_wg,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend="networkd",
            babel_rxcost=body.babel_rxcost,
            babel_type=body.babel_type,
            listen_port=body.listen_port,
            node_id=_resolve_target_node(node_id),
            render_files=False,
            allowed_ips=body.allowed_ips,
            remote_node_id=body.remote_node_id,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


@_admin_nodes_router.put("/ibgp/peers/{name}")
def api_modify_ibgp_peer(name: str, body: IbgpPeerModifyRequest, node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = modify_ibgp_peer(
            config=config,
            db_path=db_path,
            name=name,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend="networkd",
            peer_ip=body.peer_ip,
            babel_rxcost=body.babel_rxcost,
            babel_type=body.babel_type,
            listen_port=body.listen_port,
            node_id=_resolve_target_node(node_id),
            render_files=False,
            allowed_ips=body.allowed_ips,
            # 缺席时保留既有关联:proposal 接受与上报导入并不知道它的存在。
            remote_node_id=(body.remote_node_id if "remote_node_id" in body.model_fields_set else UNSET),
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


@_admin_nodes_router.delete("/ibgp/peers/{name}")
def api_delete_ibgp_peer(name: str, node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = delete_ibgp_peer(
            config=config,
            db_path=db_path,
            name=name,
            node_id=_resolve_target_node(node_id),
            render_files=False,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


# --- Admin: WireGuard tunnels (read-only) ---


@_admin_nodes_router.get("/wg/tunnels")
def api_list_wg_tunnels(live: bool = Query(False), node_id: str | None = Query(None)) -> list[dict]:
    config = _get_config()
    db_path = _get_db_path()
    try:
        tunnels = show_wg_tunnels(
            config=config, db_path=db_path, include_live=live, node_id=_resolve_target_node(node_id)
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(t) for t in tunnels]


# --- Admin: genconf ---


class GenconfRequest(BaseModel):
    overwrite_bird_conf: bool = False
    overwrite_babel_conf: bool = False


@_admin_nodes_router.post("/genconf")
def api_genconf(body: GenconfRequest) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        result = genconf(
            config=config,
            db_path=db_path,
            overwrite_bird_conf=body.overwrite_bird_conf,
            overwrite_babel_conf=body.overwrite_babel_conf,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "bird_conf_path": str(result.bird_conf_path),
        "bird_babel_conf_path": str(result.bird_babel_conf_path),
        "bird_roa_v6_conf_path": str(result.bird_roa_v6_conf_path),
        "warnings": result.warnings,
    }


# --- Public auto-peer routes (no admin/node bearer for steps 1-3) ---


_AUTO_PEER_BEARER = HTTPBearer(auto_error=False)


def _require_registry() -> AppConfig:
    config = _get_config()
    if not config.dn42_registry_path:
        raise HTTPException(
            status_code=503,
            detail="auto-peer disabled (dn42_registry_path not set)",
        )
    return config


def _map_auto_peer_error(exc: AutoPeerError) -> HTTPException:
    if isinstance(exc, AutoPeerSessionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AutoPeerExpiredError):
        return HTTPException(status_code=410, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


class AutoPeerLookupRequest(BaseModel):
    asn: int

    @field_validator("asn")
    @classmethod
    def _check_asn(cls, v: int) -> int:
        return validate_asn(v)


class AutoPeerChallengeRequest(BaseModel):
    asn: int
    mntner: str
    auth_index: int

    @field_validator("asn")
    @classmethod
    def _check_asn(cls, v: int) -> int:
        return validate_asn(v)

    @field_validator("mntner")
    @classmethod
    def _check_mntner(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("mntner 不能为空")
        return v

    @field_validator("auth_index")
    @classmethod
    def _check_idx(cls, v: int) -> int:
        if v < 0:
            raise ValueError("auth_index 必须 >= 0")
        return v


class AutoPeerVerifyRequest(BaseModel):
    challenge_id: str
    signature: str

    @field_validator("challenge_id")
    @classmethod
    def _check_cid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("challenge_id 不能为空")
        return v


class AutoPeerSubmitRequest(BaseModel):
    wg_public_key: str
    endpoint: str = ""
    peer_lla: str
    listen_port: int | None = None

    @field_validator("wg_public_key")
    @classmethod
    def _check_pubkey(cls, v: str) -> str:
        return validate_pubkey(v)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return validate_endpoint(v, allow_empty=True)

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="Peer LLA")

    @field_validator("listen_port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v


@_public_router.post("/auto-peer/lookup")
def api_auto_peer_lookup(body: AutoPeerLookupRequest) -> dict:
    config = _require_registry()
    try:
        result = start_lookup(config=config, asn=body.asn)
    except AutoPeerError as exc:
        raise _map_auto_peer_error(exc) from exc
    except Dn42CtlError as exc:
        # registry-not-found / parse errors
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "asn": result.asn,
        "mntners": [
            {
                "name": m.name,
                "auth_options": [
                    {
                        "index": opt.index,
                        "scheme": opt.scheme,
                        "fingerprint": opt.fingerprint,
                    }
                    for opt in m.auth_options
                ],
            }
            for m in result.mntners
        ],
    }


@_public_router.post("/auto-peer/challenge")
def api_auto_peer_challenge(body: AutoPeerChallengeRequest) -> dict:
    config = _require_registry()
    try:
        challenge = start_challenge(
            config=config,
            asn=body.asn,
            mntner=body.mntner,
            auth_index=body.auth_index,
        )
    except AutoPeerError as exc:
        raise _map_auto_peer_error(exc) from exc
    except Dn42CtlError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "challenge_id": challenge.challenge_id,
        "nonce": challenge.nonce_hex,
        "namespace": challenge.namespace,
        "scheme": challenge.scheme,
        "expires_in_seconds": max(0, int(challenge.expires_at - _monotonic_now())),
    }


@_public_router.post("/auto-peer/verify")
def api_auto_peer_verify(body: AutoPeerVerifyRequest) -> dict:
    config = _require_registry()
    try:
        result = verify_challenge(
            config=config,
            challenge_id=body.challenge_id,
            signature=body.signature,
        )
    except AutoPeerError as exc:
        raise _map_auto_peer_error(exc) from exc
    return {
        "peer_session_token": result.peer_session_token,
        "verified_asn": result.verified_asn,
        "verified_mntner": result.verified_mntner,
        "expires_in_seconds": max(0, int(result.expires_at - _monotonic_now())),
    }


@_public_router.post("/auto-peer/submit", status_code=201)
def api_auto_peer_submit(
    body: AutoPeerSubmitRequest,
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_AUTO_PEER_BEARER)],
) -> dict:
    config = _require_registry()
    if cred is None or not cred.credentials:
        raise HTTPException(status_code=401, detail="missing peer-session token")
    try:
        result = submit_peer(
            config=config,
            db_path=_get_db_path(),
            session_token=cred.credentials,
            wg_public_key=body.wg_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend="networkd",
            listen_port=body.listen_port,
        )
    except AutoPeerError as exc:
        raise _map_auto_peer_error(exc) from exc
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "proposal_id": result.proposal.id,
        "status": result.proposal.status,
        "node_id": result.our_node_id,
        "received_at": result.proposal.received_at,
        "message": (
            "Your peer request is pending operator approval."
            if result.proposal.status == "pending"
            else "Your peer request was processed."
        ),
    }


def _monotonic_now() -> float:
    import time

    return time.monotonic()


# Mirrors CLI `dn42ctl show`.
_show_router = APIRouter(prefix="/api/show", dependencies=[Depends(require_admin)])


@_show_router.get("/all")
def api_show_all(live: bool = Query(False), node_id: str | None = Query(None)) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    self_id = _self_node_id()
    try:
        target = _resolve_target_node(node_id)
        wg = show_wg_tunnels(config=config, db_path=db_path, include_live=live, node_id=target)
        bgp = show_bgp_peers(config=config, db_path=db_path, include_live=live, node_id=target)
        ibgp = show_ibgp_peers(config=config, db_path=db_path, include_live=live, node_id=target)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "node_id": target,
        "self_node_id": self_id,
        "config_node_id": config.node_id,
        # config.toml 的 node_id 与 self 节点 id 分叉时,admin 写入的 peer 会落在
        # desired-state 读不到的分区里。前端据此渲染警告横幅。
        "node_id_mismatch": self_id is not None and self_id != config.node_id,
        "wg": [asdict(x) for x in wg],
        "bgp": [asdict(x) for x in bgp],
        "ibgp": [asdict(x) for x in ibgp],
    }


# --- Admin: 数据库只读浏览 ---


@_admin_nodes_router.get("/db/tables")
def api_list_db_tables() -> list[dict]:
    try:
        tables = list_tables(db_path=_get_db_path())
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(t) for t in tables]


@_admin_nodes_router.get("/db/tables/{table}")
def api_browse_db_table(
    table: str,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    node_id: str | None = Query(None),
) -> dict:
    try:
        page = browse_table(
            db_path=_get_db_path(),
            table=table,
            limit=limit,
            offset=offset,
            node_id=node_id,
        )
    except Dn42CtlError as exc:
        # 未命中白名单 = 这张表不存在(对调用方而言)。不用 400,免得把"表存在但你不能看"
        # 和"表不存在"区分开来。
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return table_page_to_dict(page)


@app.get("/api/version")
def api_version() -> dict:
    from dn42ctl import __version__
    from dn42ctl._version_info import get_commit

    return {"version": __version__, "commit": get_commit()}


app.include_router(_admin_nodes_router)
app.include_router(_node_router)
app.include_router(_public_router)
app.include_router(_show_router)
