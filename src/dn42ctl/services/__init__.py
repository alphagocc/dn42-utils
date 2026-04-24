from __future__ import annotations

from .core import Dn42CtlError
from .dummy import DummyResult, ensure_dummy_interface
from .init_sys import genconf, init_node
from .bgp import create_bgp_peer, delete_bgp_peer, modify_bgp_peer
from .ibgp import create_ibgp_peer, delete_ibgp_peer, modify_ibgp_peer
from .scan import discover_bird_paths, scan_local_configs
from .show import show_bgp_peers, show_ibgp_peers, show_wg_tunnels

__all__ = [
    "Dn42CtlError",
    "DummyResult",
    "ensure_dummy_interface",
    "init_node",
    "genconf",
    "create_bgp_peer",
    "modify_bgp_peer",
    "delete_bgp_peer",
    "create_ibgp_peer",
    "modify_ibgp_peer",
    "delete_ibgp_peer",
    "discover_bird_paths",
    "scan_local_configs",
    "show_bgp_peers",
    "show_ibgp_peers",
    "show_wg_tunnels",
]
