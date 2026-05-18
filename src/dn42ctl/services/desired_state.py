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

DEFAULT_PATHS = {
    "bird_conf_path": "/etc/bird/bird.conf",
    "peers_dir": "/etc/bird/peers/",
    "babel_conf_path": "/etc/bird/babel.conf",
    "networkd_dir": "/etc/systemd/network/",
    "nm_dir": "/etc/NetworkManager/system-connections/",
}


@dataclass(frozen=True)
class DesiredState:
    node_id: str
    revision: str
    generated_at: str
    bgp_peers: list[dict[str, Any]] = field(default_factory=list)
    ibgp_peers: list[dict[str, Any]] = field(default_factory=list)
    paths: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "revision": self.revision,
            "generated_at": self.generated_at,
            "bgp_peers": list(self.bgp_peers),
            "ibgp_peers": list(self.ibgp_peers),
            "paths": dict(self.paths),
        }


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


def _compute_revision(payload_without_revision: dict[str, Any], generated_at: str) -> str:
    """Revision = timestamp + short content hash.

    Format: `<iso-utc>-<8-char hex>`.
    """
    canon = json.dumps(payload_without_revision, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]
    return f"{generated_at}-{digest}"


def build_desired_state(
    *, db_path: Path, node_id: str, record_revision: bool = True, keep_latest: int = 50
) -> DesiredState:
    """Read all peers for the given node_id from the authoritative DB and
    produce a DesiredState.

    Side effects (when `record_revision=True`):
      * Record the freshly-built revision into `config_revisions` (idempotent
        on the (node_id, revision) UNIQUE constraint).
      * Trim old revisions down to `keep_latest`.

    If `node_desired_pin` has a row for `node_id`, the pinned (older) revision
    is returned instead of the freshly computed one. This is how `rollback`
    works.
    """
    db = Database.open(db_path)
    try:
        bgp_rows = db.list_bgp_peers(node_id)
        ibgp_rows = db.list_ibgp_peers(node_id)
    finally:
        db.close()

    bgp_peers = [_bgp_row_to_dict(r) for r in bgp_rows]
    ibgp_peers = [_ibgp_row_to_dict(r) for r in ibgp_rows]
    paths = dict(DEFAULT_PATHS)
    generated_at = _now_iso()
    base = {
        "node_id": node_id,
        "bgp_peers": bgp_peers,
        "ibgp_peers": ibgp_peers,
        "paths": paths,
    }
    revision = _compute_revision(base, generated_at)

    if record_revision:
        from dn42ctl.db_managed import RevisionStore

        db = Database.open(db_path)
        try:
            store = RevisionStore(db.connection)
            store.record(
                node_id=node_id,
                revision=revision,
                generated_at=generated_at,
                payload={
                    "node_id": node_id,
                    "revision": revision,
                    "generated_at": generated_at,
                    "bgp_peers": bgp_peers,
                    "ibgp_peers": ibgp_peers,
                    "paths": paths,
                },
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
                paths=pin.payload.get("paths", {}),
            )

    return DesiredState(
        node_id=node_id,
        revision=revision,
        generated_at=generated_at,
        bgp_peers=bgp_peers,
        ibgp_peers=ibgp_peers,
        paths=paths,
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
