"""Accept / reject config proposals.

Proposals are validated and (on accept) routed to the existing service
functions: create_bgp_peer / modify_bgp_peer / delete_bgp_peer (and the iBGP
counterparts). Constraint violations from those functions surface as
Dn42CtlError, which is recorded into proposal.message and the proposal is
marked rejected.

Payload schemas (set by node-side `push`):

    kind=peer_add
        {"peer_kind": "bgp",  "peer": {peer_asn, peer_public_key, endpoint?, peer_lla,
                                       net_backend, listen_port?}}
        {"peer_kind": "ibgp", "peer": {name, peer_ip, has_wg, peer_public_key?, endpoint?,
                                       peer_lla?, net_backend?, babel_rxcost, babel_type,
                                       listen_port?}}

    kind=peer_modify (BGP): {"peer_kind": "bgp",  "peer": {peer_asn, peer_public_key, endpoint?,
                                                            peer_lla, net_backend, listen_port?}}
    kind=peer_modify (iBGP): {"peer_kind": "ibgp", "peer": {name, peer_ip, peer_public_key,
                                                             endpoint?, peer_lla?, net_backend,
                                                             babel_rxcost, babel_type, listen_port?}}

    kind=peer_delete:
        {"peer_kind": "bgp",  "key": {"peer_asn": ...}}
        {"peer_kind": "ibgp", "key": {"name": ...}}

The central host's own AppConfig is used (the proposal targets the central DB,
so the AppConfig is the server's own configuration).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import (
    ConfigProposal,
    ProposalStore,
)
from dn42ctl.services.bgp import create_bgp_peer, delete_bgp_peer, modify_bgp_peer
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.ibgp import create_ibgp_peer, delete_ibgp_peer, modify_ibgp_peer
from dn42ctl.services.peer_payload import parse_bgp_key, parse_bgp_peer, parse_ibgp_key, parse_ibgp_peer


def _require_peer_kind(payload: dict[str, Any]) -> str:
    pk = payload.get("peer_kind")
    if pk not in {"bgp", "ibgp"}:
        raise Dn42CtlError(f"payload.peer_kind 必须是 'bgp' 或 'ibgp', 收到 {pk!r}")
    return pk


def _require_peer(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise Dn42CtlError(f"payload.{field} 缺失或不是对象")
    return value


def _apply_peer_add(*, config: AppConfig, db_path: Path, target_node_id: str, payload: dict[str, Any]) -> None:
    peer_kind = _require_peer_kind(payload)
    peer = _require_peer(payload, "peer")
    if peer_kind == "bgp":
        parsed = parse_bgp_peer(peer)
        create_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=parsed.peer_asn,
            peer_public_key=parsed.peer_public_key,
            endpoint=parsed.endpoint,
            peer_lla=parsed.peer_lla,
            net_backend=parsed.net_backend,
            listen_port=parsed.listen_port,
            node_id=target_node_id,
            render_files=False,
        )
    else:
        ibgp = parse_ibgp_peer(peer)
        create_ibgp_peer(
            config=config,
            db_path=db_path,
            name=ibgp.name,
            peer_ip=ibgp.peer_ip,
            has_wg=ibgp.has_wg,
            peer_public_key=ibgp.peer_public_key,
            endpoint=ibgp.endpoint,
            peer_lla=ibgp.peer_lla,
            net_backend=ibgp.net_backend,
            babel_rxcost=ibgp.babel_rxcost,
            babel_type=ibgp.babel_type,
            listen_port=ibgp.listen_port,
            node_id=target_node_id,
            render_files=False,
        )


def _apply_peer_modify(*, config: AppConfig, db_path: Path, target_node_id: str, payload: dict[str, Any]) -> None:
    peer_kind = _require_peer_kind(payload)
    peer = _require_peer(payload, "peer")
    if peer_kind == "bgp":
        parsed = parse_bgp_peer(peer)
        modify_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=parsed.peer_asn,
            peer_public_key=parsed.peer_public_key,
            endpoint=parsed.endpoint,
            peer_lla=parsed.peer_lla,
            net_backend=parsed.net_backend,
            listen_port=parsed.listen_port,
            node_id=target_node_id,
            render_files=False,
        )
    else:
        # modify 始终按"有隧道"校验:modify_ibgp_peer 不接受 has_wg,它按库里的行判断,
        # 且拒绝 has_wg=0 的行。信 payload 的 has_wg 等于让调用方自选要不要被校验。
        ibgp = parse_ibgp_peer(peer, require_wg_fields=True)
        modify_ibgp_peer(
            config=config,
            db_path=db_path,
            name=ibgp.name,
            peer_public_key=ibgp.peer_public_key or "",
            endpoint=ibgp.endpoint or "",
            peer_lla=ibgp.peer_lla or "",
            peer_ip=ibgp.peer_ip,
            net_backend=ibgp.net_backend,
            babel_rxcost=ibgp.babel_rxcost,
            babel_type=ibgp.babel_type,
            listen_port=ibgp.listen_port,
            node_id=target_node_id,
            render_files=False,
        )


def _apply_peer_delete(*, config: AppConfig, db_path: Path, target_node_id: str, payload: dict[str, Any]) -> None:
    peer_kind = _require_peer_kind(payload)
    key = _require_peer(payload, "key")
    if peer_kind == "bgp":
        delete_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=parse_bgp_key(key),
            node_id=target_node_id,
            render_files=False,
        )
    else:
        delete_ibgp_peer(
            config=config,
            db_path=db_path,
            name=parse_ibgp_key(key),
            node_id=target_node_id,
            render_files=False,
        )


def _apply_proposal(*, config: AppConfig, db_path: Path, proposal: ConfigProposal) -> None:
    """Translate the proposal into service-layer calls.

    Writes target the proposal's own node_id (not the central self node_id) and
    skip filesystem rendering — that is the spoke's responsibility on next pull.
    """
    target = proposal.node_id
    if proposal.kind == "peer_add":
        _apply_peer_add(config=config, db_path=db_path, target_node_id=target, payload=proposal.payload)
    elif proposal.kind == "peer_modify":
        _apply_peer_modify(config=config, db_path=db_path, target_node_id=target, payload=proposal.payload)
    elif proposal.kind == "peer_delete":
        _apply_peer_delete(config=config, db_path=db_path, target_node_id=target, payload=proposal.payload)
    else:  # pragma: no cover — schema CHECK already enforces this
        raise Dn42CtlError(f"未知 proposal kind: {proposal.kind}")


def accept_proposal(
    *,
    config: AppConfig,
    db_path: Path,
    proposal_id: int,
) -> ConfigProposal:
    """Accept a proposal: run service-layer ops, then mark accepted.

    On service-layer failure the proposal stays `pending` and Dn42CtlError is
    re-raised; the caller decides whether to mark rejected (via reject_proposal).
    """
    db = Database.open(db_path)
    try:
        store = ProposalStore(db.connection)
        proposal = store.get(proposal_id)
    finally:
        db.close()
    if proposal is None:
        raise Dn42CtlError(f"proposal 不存在: {proposal_id}")
    if proposal.status != "pending":
        raise Dn42CtlError(f"proposal #{proposal_id} 当前状态为 {proposal.status}, 无法接受")
    _apply_proposal(config=config, db_path=db_path, proposal=proposal)
    db = Database.open(db_path)
    try:
        return ProposalStore(db.connection).set_status(proposal_id, "accepted")
    finally:
        db.close()


def reject_proposal(
    *,
    db_path: Path,
    proposal_id: int,
    reason: str,
) -> ConfigProposal:
    if not reason.strip():
        raise Dn42CtlError("reject 必须提供 reason")
    db = Database.open(db_path)
    try:
        store = ProposalStore(db.connection)
        proposal = store.get(proposal_id)
        if proposal is None:
            raise Dn42CtlError(f"proposal 不存在: {proposal_id}")
        if proposal.status != "pending":
            raise Dn42CtlError(f"proposal #{proposal_id} 当前状态为 {proposal.status}, 无法拒绝")
        return store.set_status(proposal_id, "rejected", message=reason)
    finally:
        db.close()


def try_auto_accept(
    *,
    config: AppConfig,
    db_path: Path,
    proposal: ConfigProposal,
    policy: dict[str, str],
) -> ConfigProposal:
    """Inspect the node's write_policy and, for an eligible proposal, immediately
    accept (or reject if the underlying service call fails).

    Currently only `peer_add` honors the auto_accept policy; `peer_modify` and
    `peer_delete` are always review-only by schema design.
    """
    if proposal.status != "pending":
        return proposal
    if proposal.kind == "peer_add" and policy.get("peer_add") == "auto_accept":
        try:
            _apply_proposal(config=config, db_path=db_path, proposal=proposal)
        except Dn42CtlError as exc:
            db = Database.open(db_path)
            try:
                return ProposalStore(db.connection).set_status(
                    proposal.id, "rejected", message=f"auto_accept 校验失败: {exc}"
                )
            finally:
                db.close()
        db = Database.open(db_path)
        try:
            return ProposalStore(db.connection).set_status(proposal.id, "accepted")
        finally:
            db.close()
    return proposal
