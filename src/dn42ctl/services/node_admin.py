"""Admin-side node management service: wraps ManagedNodeStore with token signing and
input validation. Used by both CLI (`dn42ctl node ...`) and admin REST API
(`/api/admin/nodes/...`).
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from dn42ctl.db import Database, DatabaseError
from dn42ctl.db_managed import ManagedNode, ManagedNodeStore
from dn42ctl.node_config import NodeConfig, NodeConfigError, load_node_config, save_node_config
from dn42ctl.paths import NODE_CONFIG_PATH
from dn42ctl.services.core import Dn42CtlError


def _store_for(db_path: Path) -> tuple[Database, ManagedNodeStore]:
    db = Database.open(db_path)
    return db, ManagedNodeStore(db.connection)


def _validate_node_id(node_id: str) -> str:
    try:
        uuid.UUID(node_id)
    except ValueError as exc:
        raise Dn42CtlError(f"node_id 必须是合法 UUID: {node_id}") from exc
    return node_id


def _resolve_self_toml(self_node_toml_path: Path | None) -> Path:
    return self_node_toml_path if self_node_toml_path is not None else NODE_CONFIG_PATH


def _rewrite_self_node_toml(*, path: Path, plaintext: str, node_id: str) -> bool:
    """Update token (and node_id) of an existing self node.toml.

    Returns True if the file existed and was rewritten, False if it was missing.
    """
    try:
        existing = load_node_config(path)
    except NodeConfigError:
        return False
    new_cfg = NodeConfig(
        server=existing.server,
        node_id=node_id,
        token=plaintext,
        apply_overrides=existing.apply_overrides,
        cache_db_path=existing.cache_db_path,
    )
    save_node_config(path, new_cfg)
    return True


@dataclass(frozen=True)
class RotatedToken:
    node_id: str
    plaintext: str
    self_node_toml_updated: bool = False
    self_node_toml_path: Path | None = None


def add_node(*, db_path: Path, node_id: str, name: str) -> ManagedNode:
    _validate_node_id(node_id)
    if not name.strip():
        raise Dn42CtlError("name 不能为空")
    db, store = _store_for(db_path)
    try:
        return store.add(node_id, name.strip())
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()


def list_nodes(*, db_path: Path) -> list[ManagedNode]:
    db, store = _store_for(db_path)
    try:
        return store.list_all()
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()


def get_node(*, db_path: Path, node_id: str) -> ManagedNode:
    _validate_node_id(node_id)
    db, store = _store_for(db_path)
    try:
        node = store.get(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()
    if node is None:
        raise Dn42CtlError(f"节点不存在: {node_id}")
    return node


def remove_node(
    *,
    db_path: Path,
    node_id: str,
    force: bool = False,
    self_node_toml_path: Path | None = None,
) -> ManagedNode:
    _validate_node_id(node_id)
    db, store = _store_for(db_path)
    try:
        removed = store.delete(node_id, force=force)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()
    if removed is None:
        raise Dn42CtlError(f"节点不存在: {node_id}")
    if removed.is_self:
        # When the central host's self node is force-removed, the matching
        # /etc/dn42ctl/node.toml is now stale — drop it so the next `dn42ctl serve`
        # boot re-registers a fresh self.
        path = _resolve_self_toml(self_node_toml_path)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return removed


def rotate_token(
    *,
    db_path: Path,
    node_id: str,
    self_node_toml_path: Path | None = None,
) -> RotatedToken:
    """Sign a new token. If the target node is the self node, rewrite the local
    node.toml so the next `dn42ctl node once` keeps working.
    """
    _validate_node_id(node_id)
    db, store = _store_for(db_path)
    try:
        node = store.get(node_id)
        if node is None:
            raise Dn42CtlError(f"节点不存在: {node_id}")
        plaintext = secrets.token_urlsafe(32)
        store.rotate_token(node_id, plaintext)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()

    toml_updated = False
    toml_path = None
    if node.is_self:
        toml_path = _resolve_self_toml(self_node_toml_path)
        toml_updated = _rewrite_self_node_toml(path=toml_path, plaintext=plaintext, node_id=node_id)

    return RotatedToken(
        node_id=node_id,
        plaintext=plaintext,
        self_node_toml_updated=toml_updated,
        self_node_toml_path=toml_path,
    )


@dataclass(frozen=True)
class NodeStatus:
    """Central-side view of a node's current status."""

    node_id: str
    name: str
    enabled: bool
    is_self: bool
    has_token: bool
    last_seen_at: str | None
    current_revision: str | None
    pinned_revision: str | None


def get_node_status(*, db_path: Path, node_id: str) -> NodeStatus:
    _validate_node_id(node_id)
    db, store = _store_for(db_path)
    try:
        node = store.get(node_id)
        if node is None:
            raise Dn42CtlError(f"节点不存在: {node_id}")
        # Lazy import to avoid cycles.
        from dn42ctl.db_managed import RevisionStore

        rev_store = RevisionStore(db.connection)
        revisions = rev_store.list_for_node(node_id, limit=1)
        current_revision = revisions[0].revision if revisions else None
        pin = rev_store.get_pin(node_id)
        pinned_revision = pin.revision if pin else None
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()
    return NodeStatus(
        node_id=node.node_id,
        name=node.name,
        enabled=node.enabled,
        is_self=node.is_self,
        has_token=node.api_token_hash is not None,
        last_seen_at=node.last_seen_at,
        current_revision=current_revision,
        pinned_revision=pinned_revision,
    )


def set_policy(
    *,
    db_path: Path,
    node_id: str,
    peer_add: str | None = None,
    peer_modify: str | None = None,
    peer_delete: str | None = None,
    report: str | None = None,
) -> ManagedNode:
    """Partially update write_policy; unspecified fields are preserved."""
    _validate_node_id(node_id)
    db, store = _store_for(db_path)
    try:
        node = store.get(node_id)
        if node is None:
            raise Dn42CtlError(f"节点不存在: {node_id}")
        new_policy = dict(node.write_policy)
        if peer_add is not None:
            new_policy["peer_add"] = peer_add
        if peer_modify is not None:
            new_policy["peer_modify"] = peer_modify
        if peer_delete is not None:
            new_policy["peer_delete"] = peer_delete
        if report is not None:
            new_policy["report"] = report
        try:
            return store.set_write_policy(node_id, new_policy)
        except ValueError as exc:
            raise Dn42CtlError(str(exc)) from exc
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    finally:
        db.close()
