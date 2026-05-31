from __future__ import annotations

from .bgp import create_bgp_peer, delete_bgp_peer, modify_bgp_peer
from .core import Dn42CtlError
from .desired_state import DesiredState, build_desired_state, require_managed_node_exists
from .dummy import DummyResult, ensure_dummy_interface
from .ibgp import create_ibgp_peer, delete_ibgp_peer, modify_ibgp_peer
from .init_sys import genconf, init_node
from .node_admin import (
    NodeStatus,
    RotatedToken,
    add_node,
    get_node,
    get_node_status,
    list_nodes,
    remove_node,
    rotate_token,
    set_policy,
)
from .proposal_decisions import accept_proposal, reject_proposal
from .proposals import get_proposal, list_proposals, submit_proposal
from .report_import import import_report
from .reports import get_report, list_reports, submit_report
from .revisions import clear_rollback, get_pinned, list_revisions, rollback_to
from .scan import discover_bird_paths, scan_local_configs
from .show import show_bgp_peers, show_ibgp_peers, show_wg_tunnels
from .system import (
    SystemInstallResult,
    install_firewalld_conf,
    install_nftables_conf,
    install_roa_service,
    uninstall_firewalld_conf,
    uninstall_nftables_conf,
    uninstall_roa_service,
)

__all__ = [
    "DesiredState",
    "Dn42CtlError",
    "DummyResult",
    "NodeStatus",
    "RotatedToken",
    "SystemInstallResult",
    "accept_proposal",
    "add_node",
    "build_desired_state",
    "clear_rollback",
    "create_bgp_peer",
    "create_ibgp_peer",
    "delete_bgp_peer",
    "delete_ibgp_peer",
    "discover_bird_paths",
    "ensure_dummy_interface",
    "genconf",
    "get_node",
    "get_node_status",
    "get_pinned",
    "get_proposal",
    "get_report",
    "import_report",
    "init_node",
    "install_firewalld_conf",
    "install_nftables_conf",
    "install_roa_service",
    "list_nodes",
    "list_proposals",
    "list_reports",
    "list_revisions",
    "modify_bgp_peer",
    "modify_ibgp_peer",
    "reject_proposal",
    "remove_node",
    "require_managed_node_exists",
    "rollback_to",
    "rotate_token",
    "scan_local_configs",
    "set_policy",
    "show_bgp_peers",
    "show_ibgp_peers",
    "show_wg_tunnels",
    "submit_proposal",
    "submit_report",
    "uninstall_firewalld_conf",
    "uninstall_nftables_conf",
    "uninstall_roa_service",
]
