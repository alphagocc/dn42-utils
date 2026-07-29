from __future__ import annotations

import httpx
import pytest
import respx

from dn42ctl.node_client import (
    NodeClient,
    NodeClientError,
    build_auth_headers,
    build_node_url,
    build_ws_url,
)

SERVER = "http://[::1]:4242"
NODE_ID = "node-1"
TOKEN = "mytoken"


@pytest.fixture
def client() -> NodeClient:
    return NodeClient(server=SERVER, node_id=NODE_ID, token=TOKEN)


@pytest.fixture
def mock_server():
    with respx.mock(base_url=SERVER, assert_all_called=False) as router:
        yield router


class TestPullDesired:
    def test_success(self, client: NodeClient, mock_server) -> None:
        route = mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            return_value=httpx.Response(200, json={"node_id": NODE_ID, "revision": "r1"})
        )
        payload = client.pull_desired()
        assert payload["node_id"] == NODE_ID
        assert payload["revision"] == "r1"
        # Token must be on the wire.
        assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"

    def test_401(self, client: NodeClient, mock_server) -> None:
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(return_value=httpx.Response(401))
        with pytest.raises(NodeClientError, match="401"):
            client.pull_desired()

    def test_403(self, client: NodeClient, mock_server) -> None:
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(return_value=httpx.Response(403))
        with pytest.raises(NodeClientError, match="403"):
            client.pull_desired()

    def test_5xx(self, client: NodeClient, mock_server) -> None:
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(NodeClientError, match="500"):
            client.pull_desired()

    def test_network_error(self, client: NodeClient, mock_server) -> None:
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(NodeClientError, match="无法访问"):
            client.pull_desired()


class TestPostJson:
    def test_success(self, client: NodeClient, mock_server) -> None:
        route = mock_server.post(f"/api/v1/nodes/{NODE_ID}/reports").mock(
            return_value=httpx.Response(201, json={"id": 7})
        )
        assert client.post_json("/reports", {"kind": "apply_result", "payload": {}})["id"] == 7
        assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"

    def test_401(self, client: NodeClient, mock_server) -> None:
        mock_server.post(f"/api/v1/nodes/{NODE_ID}/reports").mock(return_value=httpx.Response(401))
        with pytest.raises(NodeClientError, match="401"):
            client.post_json("/reports", {})


class TestBuildNodeUrl:
    def test_basic(self) -> None:
        assert (
            build_node_url(server="https://hub.example", node_id=NODE_ID, suffix="/desired")
            == f"https://hub.example/api/v1/nodes/{NODE_ID}/desired"
        )

    def test_strips_trailing_slash(self) -> None:
        assert (
            build_node_url(server="https://hub.example/", node_id=NODE_ID, suffix="/desired")
            == f"https://hub.example/api/v1/nodes/{NODE_ID}/desired"
        )

    def test_ipv6_literal_survives(self) -> None:
        """The self node points at http://[::1]:4242 — brackets must stay intact."""
        assert (
            build_node_url(server=SERVER, node_id=NODE_ID, suffix="/status")
            == f"http://[::1]:4242/api/v1/nodes/{NODE_ID}/status"
        )


class TestBuildWsUrl:
    def test_https_becomes_wss(self) -> None:
        assert (
            build_ws_url(server="https://hub.example", node_id=NODE_ID)
            == f"wss://hub.example/api/v1/nodes/{NODE_ID}/ws"
        )

    def test_http_becomes_ws(self) -> None:
        assert (
            build_ws_url(server="http://hub.example", node_id=NODE_ID) == f"ws://hub.example/api/v1/nodes/{NODE_ID}/ws"
        )

    def test_trailing_slash(self) -> None:
        assert (
            build_ws_url(server="https://hub.example/", node_id=NODE_ID)
            == f"wss://hub.example/api/v1/nodes/{NODE_ID}/ws"
        )

    def test_ipv6_loopback(self) -> None:
        """Self node on the hub connects over loopback, bypassing nginx."""
        assert build_ws_url(server=SERVER, node_id=NODE_ID) == f"ws://[::1]:4242/api/v1/nodes/{NODE_ID}/ws"

    def test_port_preserved(self) -> None:
        assert (
            build_ws_url(server="https://hub.example:8443", node_id=NODE_ID)
            == f"wss://hub.example:8443/api/v1/nodes/{NODE_ID}/ws"
        )


class TestBuildAuthHeaders:
    def test_bearer(self) -> None:
        assert build_auth_headers(token=TOKEN) == {"Authorization": f"Bearer {TOKEN}"}
