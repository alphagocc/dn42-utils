"""Import a scan_result report into the authoritative tables.

A scan_result payload mirrors the desired-state schema: it lists peers a node
discovered on its own filesystem. Importing turns each unknown peer into a
create_*_peer call. Already-existing peers are skipped (so this is idempotent
and safe to re-run).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dn42ctl.config import AppConfig
from dn42ctl.db import Database
from dn42ctl.db_managed import ReportStore
from dn42ctl.services.bgp import create_bgp_peer
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.ibgp import create_ibgp_peer


def _import_bgp(*, config: AppConfig, db_path: Path, target_node_id: str, peer: dict[str, Any]) -> str:
    """Returns 'created' | 'skipped'."""
    db = Database.open(db_path)
    try:
        existing = db.get_bgp_peer(target_node_id, int(peer["peer_asn"]))
    finally:
        db.close()
    if existing is not None:
        return "skipped"
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
    return "created"


def _import_ibgp(*, config: AppConfig, db_path: Path, target_node_id: str, peer: dict[str, Any]) -> str:
    _required = ("has_wg", "babel_rxcost", "babel_type")
    missing = [f for f in _required if f not in peer]
    if missing:
        raise Dn42CtlError(f"iBGP peer payload 缺少必填字段: {', '.join(missing)}")
    db = Database.open(db_path)
    try:
        existing = db.get_ibgp_peer(target_node_id, str(peer["name"]))
    finally:
        db.close()
    if existing is not None:
        return "skipped"
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
    return "created"


def import_report(
    *,
    config: AppConfig,
    db_path: Path,
    report_id: int,
) -> dict[str, int]:
    """Import a scan_result report. Writes target the report's reporting node_id
    (NOT the central self), and skips filesystem rendering (server sandbox).
    Returns counts of created/skipped per peer kind.
    """
    db = Database.open(db_path)
    try:
        store = ReportStore(db.connection)
        report = store.get(report_id)
    finally:
        db.close()
    if report is None:
        raise Dn42CtlError(f"report 不存在: {report_id}")
    if report.kind != "scan_result":
        raise Dn42CtlError(f"只能导入 kind=scan_result 的 report (当前 #{report_id} kind={report.kind})")
    if report.imported_at is not None:
        raise Dn42CtlError(f"report #{report_id} 已被导入过 (imported_at={report.imported_at})")

    target_node_id = report.node_id

    bgp_peers = report.payload.get("bgp_peers", [])
    ibgp_peers = report.payload.get("ibgp_peers", [])
    if not isinstance(bgp_peers, list) or not isinstance(ibgp_peers, list):
        raise Dn42CtlError("scan_result payload 必须含 bgp_peers / ibgp_peers 数组")

    counts = {"bgp_created": 0, "bgp_skipped": 0, "ibgp_created": 0, "ibgp_skipped": 0}
    for peer in bgp_peers:
        if not isinstance(peer, dict):
            continue
        try:
            res = _import_bgp(config=config, db_path=db_path, target_node_id=target_node_id, peer=peer)
        except Dn42CtlError as exc:
            raise Dn42CtlError(f"导入 BGP peer 失败 ({peer.get('peer_asn')}): {exc}") from exc
        counts[f"bgp_{res}"] += 1
    for peer in ibgp_peers:
        if not isinstance(peer, dict):
            continue
        try:
            res = _import_ibgp(config=config, db_path=db_path, target_node_id=target_node_id, peer=peer)
        except Dn42CtlError as exc:
            raise Dn42CtlError(f"导入 iBGP peer 失败 ({peer.get('name')}): {exc}") from exc
        counts[f"ibgp_{res}"] += 1

    db = Database.open(db_path)
    try:
        ReportStore(db.connection).mark_imported(report_id)
    finally:
        db.close()
    return counts


__all__ = ["import_report"]
