"""Node report submission and listing service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dn42ctl.db import Database
from dn42ctl.db_managed import VALID_REPORT_KINDS, ManagedNodeStore, NodeReport, ReportStore
from dn42ctl.services.core import Dn42CtlError


def submit_report(
    *,
    db_path: Path,
    node_id: str,
    kind: str,
    payload: dict[str, Any],
) -> NodeReport:
    """Persist a status report from a node. Never touches business tables."""
    if kind not in VALID_REPORT_KINDS:
        raise Dn42CtlError(f"非法 report kind: {kind} (允许: {sorted(VALID_REPORT_KINDS)})")
    if not isinstance(payload, dict):
        raise Dn42CtlError("payload 必须是对象")
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise Dn42CtlError(f"payload 无法序列化: {exc}") from exc

    db = Database.open(db_path)
    try:
        node_store = ManagedNodeStore(db.connection)
        if node_store.get(node_id) is None:
            raise Dn42CtlError(f"managed node 不存在: {node_id}")
        store = ReportStore(db.connection)
        return store.add(node_id=node_id, kind=kind, payload=payload)
    finally:
        db.close()


def list_reports(
    *,
    db_path: Path,
    node_id: str,
    kind: str | None = None,
    limit: int = 50,
) -> list[NodeReport]:
    db = Database.open(db_path)
    try:
        return ReportStore(db.connection).list_for_node(node_id, kind=kind, limit=limit)
    finally:
        db.close()


def get_report(*, db_path: Path, report_id: int) -> NodeReport:
    db = Database.open(db_path)
    try:
        r = ReportStore(db.connection).get(report_id)
    finally:
        db.close()
    if r is None:
        raise Dn42CtlError(f"report 不存在: {report_id}")
    return r
