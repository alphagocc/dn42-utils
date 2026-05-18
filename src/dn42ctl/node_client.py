"""HTTP NodeClient for spoke-side commands.

The spoke (`dn42ctl node pull/apply/push/scan/report/once`) talks to the central
server exclusively via HTTP. The self node on the central host also goes through
this client, pointing at `http://[::1]:4242` (loopback, bypassing nginx).
"""

from __future__ import annotations

from typing import Any

import httpx


class NodeClientError(RuntimeError):
    pass


class NodeClient:
    """Minimal HTTP client wrapping the node-token-authenticated endpoints.

    All requests carry `Authorization: Bearer <token>`. Server-side path-bound
    enforcement guarantees we cannot accidentally touch another node's data.
    """

    def __init__(self, *, server: str, node_id: str, token: str, timeout: float = 10.0) -> None:
        self._base = server.rstrip("/")
        self._node_id = node_id
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"}

    @property
    def node_id(self) -> str:
        return self._node_id

    def _url(self, suffix: str) -> str:
        return f"{self._base}/api/v1/nodes/{self._node_id}{suffix}"

    def pull_desired(self) -> dict[str, Any]:
        try:
            resp = httpx.get(self._url("/desired"), headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise NodeClientError(f"无法访问 server: {exc}") from exc
        if resp.status_code == 401:
            raise NodeClientError("server 拒绝鉴权 (401): 检查 node.toml 中的 token")
        if resp.status_code == 403:
            raise NodeClientError("server 返回 403: node_id 与 token 不匹配")
        if resp.status_code >= 400:
            raise NodeClientError(f"server 错误 {resp.status_code}: {resp.text}")
        return resp.json()
