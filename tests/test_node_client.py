from __future__ import annotations

import httpx
import pytest
import respx

from dn42ctl.node_client import NodeClient, NodeClientError

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
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(NodeClientError, match="500"):
            client.pull_desired()

    def test_network_error(self, client: NodeClient, mock_server) -> None:
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            side_effect=httpx.ConnectError("no route")
        )
        with pytest.raises(NodeClientError, match="无法访问"):
            client.pull_desired()
