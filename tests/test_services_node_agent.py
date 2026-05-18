from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from dn42ctl.node_config import NodeConfig
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.node_agent import pull, read_cache

SERVER = "http://[::1]:4242"
NODE_ID = "node-1"
TOKEN = "tok"


def _node_cfg(tmp_path: Path) -> NodeConfig:
    return NodeConfig(
        server=SERVER,
        node_id=NODE_ID,
        token=TOKEN,
        cache_db_path=tmp_path / "node-cache.sqlite3",
    )


def _desired_payload(revision: str = "2026-05-18T10:00:00+00:00-abcd1234") -> dict:
    return {
        "node_id": NODE_ID,
        "revision": revision,
        "generated_at": "2026-05-18T10:00:00+00:00",
        "bgp_peers": [],
        "ibgp_peers": [],
        "paths": {},
    }


@pytest.fixture
def mock_server():
    with respx.mock(base_url=SERVER, assert_all_called=False) as router:
        yield router


class TestPull:
    def test_pull_writes_cache(self, tmp_path: Path, mock_server) -> None:
        cfg = _node_cfg(tmp_path)
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            return_value=httpx.Response(200, json=_desired_payload())
        )
        result = pull(node_config=cfg)
        assert result.revision.endswith("-abcd1234")
        cached = read_cache(node_config=cfg)
        assert cached is not None
        assert cached.revision == result.revision
        assert cached.payload["node_id"] == NODE_ID

    def test_pull_replaces_old_cache(self, tmp_path: Path, mock_server) -> None:
        cfg = _node_cfg(tmp_path)
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            side_effect=[
                httpx.Response(200, json=_desired_payload("rev-1")),
                httpx.Response(200, json=_desired_payload("rev-2")),
            ]
        )
        pull(node_config=cfg)
        pull(node_config=cfg)
        cached = read_cache(node_config=cfg)
        assert cached is not None
        assert cached.revision == "rev-2"

    def test_pull_missing_revision_errors(self, tmp_path: Path, mock_server) -> None:
        cfg = _node_cfg(tmp_path)
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            return_value=httpx.Response(200, json={"node_id": NODE_ID})
        )
        with pytest.raises(Dn42CtlError, match="revision"):
            pull(node_config=cfg)

    def test_read_cache_missing(self, tmp_path: Path) -> None:
        cfg = _node_cfg(tmp_path)
        assert read_cache(node_config=cfg) is None

    def test_cache_format(self, tmp_path: Path, mock_server) -> None:
        cfg = _node_cfg(tmp_path)
        payload = _desired_payload()
        mock_server.get(f"/api/v1/nodes/{NODE_ID}/desired").mock(
            return_value=httpx.Response(200, json=payload)
        )
        pull(node_config=cfg)
        cached = read_cache(node_config=cfg)
        assert cached is not None
        # Round-trip through json must equal original.
        assert json.loads(json.dumps(cached.payload)) == payload
