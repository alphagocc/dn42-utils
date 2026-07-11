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


def _require_peer_kind(payload: dict[str, Any]) -> str:
    pk = payload.get("peer_kind")
    if pk not in {"bgp", "ibgp"}:
        raise Dn42CtlError(f"payload.peer_kind 必须是 'bgp' 或 'ibgp', 收到 {pk!r}")
    return pk


def _require_ibgp_fields(peer: dict[str, Any], fields: list[str]) -> None:
    missing = [f for f in fields if f not in peer]
    if missing:
        raise Dn42CtlError(f"iBGP peer payload 缺少必填字段: {', '.join(missing)}")


def _apply_peer_add(*, config: AppConfig, db_path: Path, target_node_id: str, payload: dict[str, Any]) -> None:
    peer_kind = _require_peer_kind(payload)
    peer = payload.get("peer")
    if not isinstance(peer, dict):
        raise Dn42CtlError("payload.peer 缺失或不是对象")
    if peer_kind == "bgp":
        create_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=int(peer["peer_asn"]),
            peer_public_key=str(peer["peer_public_key"]),
            endpoint=str(peer.get("endpoint") or ""),
            peer_lla=str(peer["peer_lla"]),
            net_backend=str(peer.get("net_backend") or "networkd"),
            listen_port=peer.get("listen_port"),
            node_id=target_node_id,
            render_files=False,
        )
    else:
        _require_ibgp_fields(peer, ["has_wg", "babel_rxcost", "babel_type"])
        create_ibgp_peer(
            config=config,
            db_path=db_path,
            name=str(peer["name"]),
            peer_ip=str(peer["peer_ip"]),
            has_wg=bool(peer["has_wg"]),
            peer_public_key=peer.get("peer_public_key"),
            endpoint=peer.get("endpoint"),
            peer_lla=peer.get("peer_lla"),
            net_backend=peer.get("net_backend"),
            babel_rxcost=int(peer["babel_rxcost"]),
            babel_type=str(peer["babel_type"]),
            listen_port=peer.get("listen_port"),
            node_id=target_node_id,
            render_files=False,
        )


def _apply_peer_modify(*, config: AppConfig, db_path: Path, target_node_id: str, payload: dict[str, Any]) -> None:
    peer_kind = _require_peer_kind(payload)
    peer = payload.get("peer")
    if not isinstance(peer, dict):
        raise Dn42CtlError("payload.peer 缺失或不是对象")
    if peer_kind == "bgp":
        modify_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=int(peer["peer_asn"]),
            peer_public_key=str(peer["peer_public_key"]),
            endpoint=str(peer.get("endpoint") or ""),
            peer_lla=str(peer["peer_lla"]),
            net_backend=str(peer.get("net_backend") or "networkd"),
            listen_port=peer.get("listen_port"),
            node_id=target_node_id,
            render_files=False,
        )
    else:
        _require_ibgp_fields(peer, ["babel_rxcost", "babel_type"])
        modify_ibgp_peer(
            config=config,
            db_path=db_path,
            name=str(peer["name"]),
            peer_public_key=str(peer["peer_public_key"]),
            endpoint=str(peer.get("endpoint") or ""),
            peer_lla=str(peer.get("peer_lla") or ""),
            peer_ip=str(peer["peer_ip"]),
            net_backend=str(peer.get("net_backend") or "networkd"),
            babel_rxcost=int(peer["babel_rxcost"]),
            babel_type=str(peer["babel_type"]),
            listen_port=peer.get("listen_port"),
            node_id=target_node_id,
            render_files=False,
        )


def _apply_peer_delete(*, config: AppConfig, db_path: Path, target_node_id: str, payload: dict[str, Any]) -> None:
    peer_kind = _require_peer_kind(payload)
    key = payload.get("key")
    if not isinstance(key, dict):
        raise Dn42CtlError("payload.key 缺失或不是对象")
    if peer_kind == "bgp":
        delete_bgp_peer(
            config=config,
            db_path=db_path,
            peer_asn=int(key["peer_asn"]),
            node_id=target_node_id,
            render_files=False,
        )
    else:
        delete_ibgp_peer(
            config=config,
            db_path=db_path,
            name=str(key["name"]),
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
