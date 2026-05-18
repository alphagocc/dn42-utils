"""Admin-side revision listing and rollback."""

from __future__ import annotations

from pathlib import Path

from dn42ctl.db import Database
from dn42ctl.db_managed import ConfigRevision, RevisionStore
from dn42ctl.services.core import Dn42CtlError


def list_revisions(*, db_path: Path, node_id: str, limit: int = 50) -> list[ConfigRevision]:
    db = Database.open(db_path)
    try:
        return RevisionStore(db.connection).list_for_node(node_id, limit=limit)
    finally:
        db.close()


def get_pinned(*, db_path: Path, node_id: str) -> ConfigRevision | None:
    db = Database.open(db_path)
    try:
        return RevisionStore(db.connection).get_pin(node_id)
    finally:
        db.close()


def rollback_to(*, db_path: Path, node_id: str, revision: str) -> ConfigRevision:
    """Set `revision` as the desired revision for `node_id`.

    Next time the node pulls, it receives this revision instead of the freshly
    computed one. Raises if the revision doesn't exist for that node.
    """
    db = Database.open(db_path)
    try:
        store = RevisionStore(db.connection)
        target = store.get_by_revision(node_id, revision)
        if target is None:
            raise Dn42CtlError(f"revision 不存在: node={node_id} revision={revision}")
        store.pin(node_id, revision)
        return target
    finally:
        db.close()


def clear_rollback(*, db_path: Path, node_id: str) -> None:
    """Remove the pin so that the node receives the latest revision again."""
    db = Database.open(db_path)
    try:
        RevisionStore(db.connection).unpin(node_id)
    finally:
        db.close()
