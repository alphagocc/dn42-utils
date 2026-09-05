from __future__ import annotations

import httpx
import pytest
import respx

from dn42ctl.node_client import (
    NodeClient,
    NodeClientError,
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

    @pytest.mark.parametrize(
        ("status", "body"),
        [(401, ""), (403, ""), (500, "boom")],
        ids=["unauthorized", "forbidden", "server-error"],
    )
    def test_http_errors(self, client: NodeClient, mock_server, status: int, body: str) -> None:
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(return_value=httpx.Response(status, text=body))
        with pytest.raises(NodeClientError, match=str(status)):
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
    @pytest.mark.parametrize(
        ("server", "suffix", "expected"),
        [
            ("https://hub.example", "/desired", f"https://hub.example/api/v1/nodes/{NODE_ID}/desired"),
            ("https://hub.example/", "/desired", f"https://hub.example/api/v1/nodes/{NODE_ID}/desired"),
            (SERVER, "/status", f"http://[::1]:4242/api/v1/nodes/{NODE_ID}/status"),
        ],
        ids=["https", "trailing-slash", "ipv6-loopback"],
    )
    def test_node_url(self, server: str, suffix: str, expected: str) -> None:
        """The self node points at http://[::1]:4242 — brackets must stay intact."""
        assert build_node_url(server=server, node_id=NODE_ID, suffix=suffix) == expected


class TestBuildWsUrl:
    @pytest.mark.parametrize(
        ("server", "expected"),
        [
            ("https://hub.example", f"wss://hub.example/api/v1/nodes/{NODE_ID}/ws"),
            ("http://hub.example", f"ws://hub.example/api/v1/nodes/{NODE_ID}/ws"),
            ("https://hub.example/", f"wss://hub.example/api/v1/nodes/{NODE_ID}/ws"),
            (SERVER, f"ws://[::1]:4242/api/v1/nodes/{NODE_ID}/ws"),
            ("https://hub.example:8443", f"wss://hub.example:8443/api/v1/nodes/{NODE_ID}/ws"),
        ],
        ids=["https", "http", "trailing-slash", "ipv6-loopback", "explicit-port"],
    )
    def test_ws_url(self, server: str, expected: str) -> None:
        """Self node on the hub connects over loopback, bypassing nginx."""
        assert build_ws_url(server=server, node_id=NODE_ID) == expected
