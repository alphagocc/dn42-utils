"""HTTP/WS transport helpers for spoke-side commands.

The spoke talks to the central server over two channels that share one token and
one URL scheme:

  * HTTP — one-shot CLI commands (`node pull/apply/once/push/report/status`),
    kept for manual troubleshooting.
  * WebSocket — the resident `dn42ctl node agent`, see
    `docs/architecture/sync_ws_protocol.md`.

The self node on the central host uses the same code, pointing at
`http://[::1]:4242` (loopback, bypassing nginx).
"""

from __future__ import annotations

from typing import Any

import httpx


class NodeClientError(RuntimeError):
    pass


def build_node_url(*, server: str, node_id: str, suffix: str) -> str:
    """`{server}/api/v1/nodes/{node_id}{suffix}`.

    Plain string surgery rather than `urljoin`, so the self node's IPv6 literal
    (`http://[::1]:4242`) survives intact.
    """
    return f"{server.rstrip('/')}/api/v1/nodes/{node_id}{suffix}"


def build_ws_url(*, server: str, node_id: str) -> str:
    """WebSocket endpoint for the resident agent. http -> ws, https -> wss."""
    base = server.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/api/v1/nodes/{node_id}/ws"


def build_auth_headers(*, token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def raise_for_status(resp: httpx.Response) -> None:
    """Single place mapping server status codes to NodeClientError.

    Mirrors the WebSocket close codes 4401/4403 documented in
    `docs/architecture/sync_ws_protocol.md`.
    """
    if resp.status_code == 401:
        raise NodeClientError("server 拒绝鉴权 (401): 检查 node.toml 中的 token")
    if resp.status_code == 403:
        raise NodeClientError("server 返回 403: node_id 与 token 不匹配")
    if resp.status_code >= 400:
        raise NodeClientError(f"server 错误 {resp.status_code}: {resp.text}")


class NodeClient:
    """Minimal HTTP client wrapping the node-token-authenticated endpoints.

    All requests carry `Authorization: Bearer <token>`. Server-side path-bound
    enforcement guarantees we cannot accidentally touch another node's data.
    """

    def __init__(self, *, server: str, node_id: str, token: str, timeout: float = 10.0) -> None:
        self._base = server.rstrip("/")
        self._node_id = node_id
        self._timeout = timeout
        self._headers = build_auth_headers(token=token)

    @property
    def node_id(self) -> str:
        return self._node_id

    def _url(self, suffix: str) -> str:
        return build_node_url(server=self._base, node_id=self._node_id, suffix=suffix)

    def _request(self, method: str, suffix: str, *, json_body: Any = None, timeout: float | None = None) -> Any:
        try:
            resp = httpx.request(
                method,
                self._url(suffix),
                headers=self._headers,
                json=json_body,
                timeout=self._timeout if timeout is None else timeout,
            )
        except httpx.HTTPError as exc:
            raise NodeClientError(f"无法访问 server: {exc}") from exc
        raise_for_status(resp)
        return resp.json()

    def pull_desired(self) -> dict[str, Any]:
        return self._request("GET", "/desired")

    def fetch_status(self, *, timeout: float | None = None) -> dict[str, Any]:
        """GET /status. Uses a shorter timeout by default (status is a probe)."""
        eff_timeout = timeout if timeout is not None else min(self._timeout, 5.0)
        return self._request("GET", "/status", timeout=eff_timeout)

    def post_json(self, suffix: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", suffix, json_body=body)
