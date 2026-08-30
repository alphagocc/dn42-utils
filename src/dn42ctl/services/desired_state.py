"""Build the desired-state JSON for a given managed node.

This module is the single source of truth for what `/api/v1/nodes/{id}/desired`
returns and what `dn42ctl node apply` should render. The output schema is
documented in `docs/architecture/sync_hub_spoke.md`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dn42ctl.db import Database
from dn42ctl.services.core import Dn42CtlError, parse_allowed_ips_json


@dataclass(frozen=True)
class DesiredState:
    node_id: str
    revision: str
    generated_at: str
    bgp_peers: list[dict[str, Any]] = field(default_factory=list)
    ibgp_peers: list[dict[str, Any]] = field(default_factory=list)
    # No file locations here. Where a node writes bird.conf / peers / netdev files is
    # that machine's own property: the hub does not know it and has no business
    # dictating it. `node_apply._resolve_paths` resolves it locally from node.toml
    # [apply], the node's config.toml and the built-in defaults.
    #
    # The node's own address block (own_ipv6 / router_id). A NULL column is left out
    # of the dict; an empty block means the hub does not manage this node's addresses,
    # and apply leaves config.toml / bird.conf alone.
    node: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "node_id": self.node_id,
            "revision": self.revision,
            "generated_at": self.generated_at,
            "bgp_peers": list(self.bgp_peers),
            "ibgp_peers": list(self.ibgp_peers),
        }
        # An empty block stays out of the payload; see the no-churn rule in
        # compute_content_digest.
        if self.node:
            out["node"] = dict(self.node)
        return out


def _node_block(row: Any) -> dict[str, Any]:
    """The node's own address block, carrying only the non-NULL fields.

    NULL means the hub does not manage that field: it is not pushed, and whatever the
    node already has in its config.toml is left untouched.

    endpoint_host is deliberately never pushed. A node does not dial itself, so apply
    would have nothing to do with it, and every field in the desired state has to have
    a well-defined effect on the spoke.
    """
    out: dict[str, Any] = {}
    if row is None:
        return out
    if row.own_ipv6:
        out["own_ipv6"] = row.own_ipv6
    if row.router_id:
        out["router_id"] = row.router_id
    return out


def _load_node_block(db: Database, node_id: str) -> dict[str, Any]:
    from dn42ctl.db_managed import ManagedNodeStore

    return _node_block(ManagedNodeStore(db.connection).get(node_id))


def _bgp_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "peer_asn": int(row["peer_asn"]),
        "ifname": row["ifname"],
        "wg_private_key": row["wg_private_key"],
        "wg_public_key": row["wg_public_key"],
        "peer_public_key": row["peer_public_key"],
        "endpoint": row["endpoint"],
        "local_lla": row["local_lla"],
        "peer_lla": row["peer_lla"],
        "listen_port": int(row["listen_port"]),
        "allowed_ips": parse_allowed_ips_json(row["allowed_ips_json"]),
        "net_backend": row["net_backend"],
    }


def _ibgp_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "name": row["name"],
        "ifname": row["ifname"],
        "wg_private_key": row["wg_private_key"],
        "wg_public_key": row["wg_public_key"],
        "peer_public_key": row["peer_public_key"],
        "endpoint": row["endpoint"],
        "local_lla": row["local_lla"],
        "peer_lla": row["peer_lla"],
        "peer_ip": row["peer_ip"],
        "has_wg": bool(row["has_wg"]),
        "listen_port": int(row["listen_port"]),
        "allowed_ips": parse_allowed_ips_json(row["allowed_ips_json"]),
        "net_backend": row["net_backend"],
        "babel_rxcost": int(row["babel_rxcost"]),
        "babel_type": row["babel_type"],
    }


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def compute_content_digest(
    *,
    node_id: str,
    bgp_peers: list[dict[str, Any]],
    ibgp_peers: list[dict[str, Any]],
    node: dict[str, Any] | None = None,
) -> str:
    """8-char hex digest of everything that makes a desired state distinct.

    Deliberately excludes `generated_at` — two builds of identical content produce
    the same digest even though their revision strings differ. This is what lets
    the hub answer "did anything actually change?" without writing to the DB.

    The no-churn rule: `node` only enters the canonical JSON when it is non-empty.
    That is correctness, not an optimisation. Adding the key unconditionally would
    change the content hash of every node in the mesh the moment this ships, so every
    node would receive one meaningless push and write one config_revisions row. With
    the condition, a fleet that does not use the feature stays byte-for-byte the same.
    """
    canon_obj: dict[str, Any] = {
        "node_id": node_id,
        "bgp_peers": bgp_peers,
        "ibgp_peers": ibgp_peers,
    }
    if node:
        canon_obj["node"] = node
    canon = json.dumps(canon_obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]


def _compute_revision(payload_without_revision: dict[str, Any], generated_at: str) -> str:
    """Revision = timestamp + short content hash.

    Format: `<iso-utc>-<8-char hex>`.
    """
    canon = json.dumps(payload_without_revision, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]
    return f"{generated_at}-{digest}"


def digest_of_revision(revision: str) -> str | None:
    """Pull the content digest back out of a revision string.

    The timestamp half contains `-` (ISO dates do), so split from the right.
    Returns None for anything that doesn't look like our format.
    """
    _, sep, digest = revision.rpartition("-")
    return digest if sep and digest else None


@dataclass(frozen=True)
class DesiredFingerprint:
    """Cheap, read-only answer to "what would this node's desired state hash to?"

    Built without writing a single row — no `config_revisions` insert, no `trim`.
    The hub compares `content_hash` against what it last pushed on a connection to
    decide whether a push is warranted at all.
    """

    node_id: str
    content_hash: str
    pinned_revision: str | None


def compute_desired_fingerprint(*, db_path: Path, node_id: str) -> DesiredFingerprint:
    """Fingerprint the payload this node would actually receive.

    Note this is NOT `build_desired_state(record_revision=False)`: that path skips
    the pin lookup entirely and would report the *live* content for a node that has
    been rolled back, causing the hub to push a state the node should not get.
    """
    from dn42ctl.db_managed import RevisionStore

    db = Database.open(db_path)
    try:
        bgp_rows = db.list_bgp_peers(node_id)
        ibgp_rows = db.list_ibgp_peers(node_id)
        node_block = _load_node_block(db, node_id)
        pin = RevisionStore(db.connection).get_pin(node_id)
    finally:
        db.close()

    if pin is not None:
        payload = pin.payload
        return DesiredFingerprint(
            node_id=node_id,
            content_hash=compute_content_digest(
                node_id=payload.get("node_id", node_id),
                bgp_peers=payload.get("bgp_peers", []),
                ibgp_peers=payload.get("ibgp_peers", []),
                # Older snapshots do not carry this key.
                node=payload.get("node", {}),
            ),
            pinned_revision=pin.revision,
        )

    return DesiredFingerprint(
        node_id=node_id,
        content_hash=compute_content_digest(
            node_id=node_id,
            bgp_peers=[_bgp_row_to_dict(r) for r in bgp_rows],
            ibgp_peers=[_ibgp_row_to_dict(r) for r in ibgp_rows],
            node=node_block,
        ),
        pinned_revision=None,
    )


def build_desired_state(
    *, db_path: Path, node_id: str, record_revision: bool = True, keep_latest: int = 50
) -> DesiredState:
    """Read all peers for the given node_id from the authoritative DB and
    produce a DesiredState.

    Side effects (when `record_revision=True`):
      * Record the freshly-built revision into `config_revisions`, but only when
        its content actually differs from the newest recorded one. Because the
        revision string embeds `generated_at`, a naive record-every-time would
        write a row on every single build — the agent's periodic reconcile alone
        would then evict the rollback history within hours.
      * Trim old revisions down to `keep_latest`.

    If `node_desired_pin` has a row for `node_id`, the pinned (older) revision
    is returned instead of the freshly computed one. This is how `rollback`
    works.
    """
    db = Database.open(db_path)
    try:
        bgp_rows = db.list_bgp_peers(node_id)
        ibgp_rows = db.list_ibgp_peers(node_id)
        node_block = _load_node_block(db, node_id)
    finally:
        db.close()

    bgp_peers = [_bgp_row_to_dict(r) for r in bgp_rows]
    ibgp_peers = [_ibgp_row_to_dict(r) for r in ibgp_rows]
    generated_at = _now_iso()
    base: dict[str, Any] = {
        "node_id": node_id,
        "bgp_peers": bgp_peers,
        "ibgp_peers": ibgp_peers,
    }
    # Must match compute_content_digest: an empty block stays out of the canonical
    # JSON, otherwise every node's revision would change the moment this ships.
    if node_block:
        base["node"] = node_block
    revision = _compute_revision(base, generated_at)

    if record_revision:
        from dn42ctl.db_managed import RevisionStore

        db = Database.open(db_path)
        try:
            store = RevisionStore(db.connection)
            previous = store.latest_revision(node_id)
            if previous is not None and digest_of_revision(previous) == digest_of_revision(revision):
                # Identical content to the last snapshot. Reuse that snapshot's
                # revision string and timestamp verbatim so the identifier stays
                # stable across rebuilds, and skip the write entirely.
                existing = store.get_by_revision(node_id, previous)
                if existing is not None:
                    revision = existing.revision
                    generated_at = existing.generated_at
            else:
                payload: dict[str, Any] = {
                    "node_id": node_id,
                    "revision": revision,
                    "generated_at": generated_at,
                    "bgp_peers": bgp_peers,
                    "ibgp_peers": ibgp_peers,
                }
                if node_block:
                    payload["node"] = node_block
                store.record(
                    node_id=node_id,
                    revision=revision,
                    generated_at=generated_at,
                    payload=payload,
                )
                store.trim(node_id, keep_latest=keep_latest)
            pin = store.get_pin(node_id)
        finally:
            db.close()
        if pin is not None:
            # Return the pinned revision payload verbatim.
            return DesiredState(
                node_id=pin.payload["node_id"],
                revision=pin.payload["revision"],
                generated_at=pin.payload["generated_at"],
                bgp_peers=pin.payload.get("bgp_peers", []),
                ibgp_peers=pin.payload.get("ibgp_peers", []),
                # Older snapshots do not carry this key.
                node=pin.payload.get("node", {}),
            )

    return DesiredState(
        node_id=node_id,
        revision=revision,
        generated_at=generated_at,
        bgp_peers=bgp_peers,
        ibgp_peers=ibgp_peers,
        node=node_block,
    )


def require_managed_node_exists(*, db_path: Path, node_id: str) -> None:
    """Sanity check before generating desired state: the node must be registered."""
    from dn42ctl.db_managed import ManagedNodeStore

    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        node = store.get(node_id)
    finally:
        db.close()
    if node is None:
        raise Dn42CtlError(f"managed node 不存在: {node_id}")
