from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.node_config import (
    NODE_CACHE_DB_PATH,
    NodeConfig,
    NodeConfigError,
    load_node_config,
    save_node_config,
)


class TestSaveLoadRoundTrip:
    def test_minimal(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        save_node_config(
            path,
            NodeConfig(server="http://[::1]:4242", node_id="abc", token="tok"),
        )
        loaded = load_node_config(path)
        assert loaded.server == "http://[::1]:4242"
        assert loaded.node_id == "abc"
        assert loaded.token == "tok"
        assert loaded.apply_overrides == {}
        assert loaded.cache_db_path == NODE_CACHE_DB_PATH

    def test_with_apply_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        peers = tmp_path / "peers"
        babel = tmp_path / "babel"
        save_node_config(
            path,
            NodeConfig(
                server="https://center.example",
                node_id="abc",
                token="tok",
                apply_overrides={"bird_peers_dir": str(peers), "babel_conf_path": str(babel)},
            ),
        )
        loaded = load_node_config(path)
        assert loaded.apply_overrides["bird_peers_dir"] == str(peers)
        assert loaded.apply_overrides["babel_conf_path"] == str(babel)

    def test_with_cache_db_override(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        cache = tmp_path / "cache.sqlite3"
        save_node_config(
            path,
            NodeConfig(server="x", node_id="y", token="z", cache_db_path=cache),
        )
        loaded = load_node_config(path)
        assert loaded.cache_db_path == cache


class TestLoadErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(NodeConfigError, match="不存在"):
            load_node_config(tmp_path / "nope.toml")

    def test_missing_server(self, tmp_path: Path) -> None:
        p = tmp_path / "node.toml"
        p.write_text('node_id = "x"\ntoken = "y"\n', encoding="utf-8")
        with pytest.raises(NodeConfigError, match="server"):
            load_node_config(p)

    def test_apply_block_not_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "node.toml"
        p.write_text(
            'server = "s"\nnode_id = "x"\ntoken = "y"\napply = "bogus"\n',
            encoding="utf-8",
        )
        with pytest.raises(NodeConfigError, match="apply"):
            load_node_config(p)
