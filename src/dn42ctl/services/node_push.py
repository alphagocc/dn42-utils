"""Spoke-side helpers for posting proposals and reports.

The "scan/push/report" CLI commands sit on top of these. The pre-built
proposal/report payloads are passed in directly; how they're constructed
(from a JSON file, from a scan of the local filesystem, etc.) is left to
the CLI layer.
"""

from __future__ import annotations

from typing import Any

from dn42ctl.node_client import NodeClient, NodeClientError
from dn42ctl.node_config import NodeConfig
from dn42ctl.services.core import Dn42CtlError


def post_proposal(
    *,
    node_config: NodeConfig,
    kind: str,
    payload: dict[str, Any],
    source: str = "push",
) -> dict[str, Any]:
    return _post_json(
        node_config=node_config,
        suffix="/proposals",
        body={"source": source, "kind": kind, "payload": payload},
        error_label="proposal",
    )


def post_report(
    *,
    node_config: NodeConfig,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return _post_json(
        node_config=node_config,
        suffix="/reports",
        body={"kind": kind, "payload": payload},
        error_label="report",
    )


def _post_json(
    *,
    node_config: NodeConfig,
    suffix: str,
    body: dict[str, Any],
    error_label: str,
) -> dict[str, Any]:
    base = node_config.server.rstrip("/")
    url = f"{base}/api/v1/nodes/{node_config.node_id}{suffix}"
    headers = {"Authorization": f"Bearer {node_config.token}"}
    import httpx

    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=10.0)
    except httpx.HTTPError as exc:
        raise Dn42CtlError(f"无法访问 server: {exc}") from exc

    if resp.status_code == 401:
        raise NodeClientError("server 拒绝鉴权 (401): 检查 node.toml 中的 token")
    if resp.status_code == 403:
        raise NodeClientError("server 返回 403: node_id 与 token 不匹配")
    if resp.status_code >= 400:
        raise Dn42CtlError(f"server 错误 {resp.status_code}: {resp.text}")
    return resp.json()


# Helpers to construct proposal payloads from a desired-state-style peer dict.


def build_peer_add_payload(*, peer_kind: str, peer: dict[str, Any]) -> dict[str, Any]:
    """Wrap a peer dict for a peer_add proposal."""
    if peer_kind not in {"bgp", "ibgp"}:
        raise Dn42CtlError(f"peer_kind 必须是 bgp 或 ibgp, 收到 {peer_kind}")
    return {"peer_kind": peer_kind, "peer": peer}


__all__ = [
    "NodeClient",  # re-export for convenience
    "build_peer_add_payload",
    "post_proposal",
    "post_report",
]
