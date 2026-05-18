"""Admin-side proposal management service.

Node-side push/scan handling is split across `node_push.py` (stage 3) and
`node_admin_proposals.py` (stage 4 accept/reject). This module is the
server-side handler invoked by both the REST API and the admin CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dn42ctl.db import Database
from dn42ctl.db_managed import (
    VALID_PROPOSAL_KINDS,
    ConfigProposal,
    ManagedNodeStore,
    ProposalStore,
)
from dn42ctl.services.core import Dn42CtlError


def submit_proposal(
    *,
    db_path: Path,
    node_id: str,
    source: str,
    kind: str,
    payload: dict[str, Any],
) -> ConfigProposal:
    """Persist a proposal from a node (or scan). Stage 3 only writes; auto-accept
    handling (write_policy.peer_add=auto_accept) is wired in stage 4.
    """
    if kind not in VALID_PROPOSAL_KINDS:
        raise Dn42CtlError(f"非法 kind: {kind} (允许: {sorted(VALID_PROPOSAL_KINDS)})")
    if source not in {"push", "scan"}:
        raise Dn42CtlError(f"非法 source: {source}")
    if not isinstance(payload, dict):
        raise Dn42CtlError("payload 必须是对象")
    # Validate payload is JSON-serializable.
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise Dn42CtlError(f"payload 无法序列化: {exc}") from exc

    db = Database.open(db_path)
    try:
        node_store = ManagedNodeStore(db.connection)
        if node_store.get(node_id) is None:
            raise Dn42CtlError(f"managed node 不存在: {node_id}")
        store = ProposalStore(db.connection)
        return store.add(node_id=node_id, source=source, kind=kind, payload=payload)
    finally:
        db.close()


def list_proposals(
    *,
    db_path: Path,
    node_id: str,
    status: str | None = None,
    limit: int = 200,
) -> list[ConfigProposal]:
    db = Database.open(db_path)
    try:
        return ProposalStore(db.connection).list_for_node(node_id, status=status, limit=limit)
    finally:
        db.close()


def get_proposal(*, db_path: Path, proposal_id: int) -> ConfigProposal:
    db = Database.open(db_path)
    try:
        p = ProposalStore(db.connection).get(proposal_id)
    finally:
        db.close()
    if p is None:
        raise Dn42CtlError(f"proposal 不存在: {proposal_id}")
    return p
