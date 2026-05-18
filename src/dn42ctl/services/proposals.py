"""Admin-side proposal management service.

Stage 4 wires auto-accept: when `submit_proposal` is called and the node's
write_policy.peer_add == "auto_accept", the proposal is immediately accepted
(or rejected with a reason) right after insertion. peer_modify / peer_delete
are always review by schema design.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dn42ctl.config import AppConfig
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
    config: AppConfig | None = None,
) -> ConfigProposal:
    """Persist a proposal. If the node's write_policy authorizes auto-accept
    for this kind AND `config` is provided, immediately run the underlying
    service call (and mark accepted, or rejected on failure).
    """
    if kind not in VALID_PROPOSAL_KINDS:
        raise Dn42CtlError(f"非法 kind: {kind} (允许: {sorted(VALID_PROPOSAL_KINDS)})")
    if source not in {"push", "scan"}:
        raise Dn42CtlError(f"非法 source: {source}")
    if not isinstance(payload, dict):
        raise Dn42CtlError("payload 必须是对象")
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise Dn42CtlError(f"payload 无法序列化: {exc}") from exc

    db = Database.open(db_path)
    try:
        node_store = ManagedNodeStore(db.connection)
        node = node_store.get(node_id)
        if node is None:
            raise Dn42CtlError(f"managed node 不存在: {node_id}")
        store = ProposalStore(db.connection)
        proposal = store.add(node_id=node_id, source=source, kind=kind, payload=payload)
        policy = node.write_policy
    finally:
        db.close()

    if config is not None:
        # Late import to avoid an import cycle between proposals.py and
        # proposal_decisions.py (which itself imports services for create/modify).
        from dn42ctl.services.proposal_decisions import try_auto_accept

        proposal = try_auto_accept(config=config, db_path=db_path, proposal=proposal, policy=policy)
    return proposal


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
