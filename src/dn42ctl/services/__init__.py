from __future__ import annotations

from .bgp import create_bgp_peer, delete_bgp_peer, modify_bgp_peer
from .core import Dn42CtlError
from .desired_state import DesiredState, build_desired_state, require_managed_node_exists
from .dummy import DummyResult, ensure_dummy_interface
from .ibgp import create_ibgp_peer, delete_ibgp_peer, modify_ibgp_peer
from .init_sys import genconf, init_node
from .node_admin import (
    RotatedToken,
    add_node,
    get_node,
    list_nodes,
    remove_node,
    rotate_token,
    set_policy,
)
from .node_push import build_peer_add_payload, post_proposal, post_report
from .proposal_decisions import accept_proposal, reject_proposal
from .proposals import get_proposal, list_proposals, submit_proposal
from .report_import import import_report
from .reports import get_report, list_reports, submit_report
from .scan import discover_bird_paths, scan_local_configs
from .show import show_bgp_peers, show_ibgp_peers, show_wg_tunnels

__all__ = [
    "DesiredState",
    "Dn42CtlError",
    "DummyResult",
    "RotatedToken",
    "accept_proposal",
    "add_node",
    "build_desired_state",
    "build_peer_add_payload",
    "create_bgp_peer",
    "create_ibgp_peer",
    "delete_bgp_peer",
    "delete_ibgp_peer",
    "discover_bird_paths",
    "ensure_dummy_interface",
    "genconf",
    "get_node",
    "get_proposal",
    "get_report",
    "import_report",
    "init_node",
    "list_nodes",
    "list_proposals",
    "list_reports",
    "modify_bgp_peer",
    "modify_ibgp_peer",
    "post_proposal",
    "post_report",
    "reject_proposal",
    "remove_node",
    "require_managed_node_exists",
    "rotate_token",
    "scan_local_configs",
    "set_policy",
    "show_bgp_peers",
    "show_ibgp_peers",
    "show_wg_tunnels",
    "submit_proposal",
    "submit_report",
]
