from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.node_config import (
    DEFAULT_AGENT_OPTIONS,
    NODE_CACHE_DB_PATH,
    AgentOptions,
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
        assert loaded.cache_db_path == NODE_CACHE_DB_PATH

    def test_rejects_stale_location_keys(self, tmp_path: Path) -> None:
        """写入位置改由本机 config.toml 的 [paths] 决定。留在 node.toml 里的旧键
        被忽略的话,操作员会以为它还生效。"""
        path = tmp_path / "node.toml"
        path.write_text('server = "http://x"\nnode_id = "n"\ntoken = "t"\n\n[apply]\npeers_dir = "/etc/bird/peers"\n')
        with pytest.raises(NodeConfigError, match="peers_dir"):
            load_node_config(path)

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


def _write(path: Path, agent_block: str = "") -> Path:
    path.write_text(f'server = "s"\nnode_id = "x"\ntoken = "y"\n{agent_block}', encoding="utf-8")
    return path


class TestAgentOptions:
    def test_defaults_when_absent(self, tmp_path: Path) -> None:
        cfg = load_node_config(_write(tmp_path / "node.toml"))
        assert cfg.agent == DEFAULT_AGENT_OPTIONS
        assert cfg.agent.reconnect_initial_seconds == 1.0
        assert cfg.agent.reconnect_max_seconds == 60.0
        assert cfg.agent.auth_retry_seconds == 300.0
        assert cfg.agent.reconcile_interval_seconds == 900.0
        assert cfg.agent.heartbeat_interval_seconds == 60.0

    def test_partial_override_keeps_other_defaults(self, tmp_path: Path) -> None:
        cfg = load_node_config(_write(tmp_path / "node.toml", "[agent]\nreconcile_interval_seconds = 120\n"))
        assert cfg.agent.reconcile_interval_seconds == 120.0
        assert cfg.agent.reconnect_max_seconds == DEFAULT_AGENT_OPTIONS.reconnect_max_seconds

    def test_integers_are_accepted_as_floats(self, tmp_path: Path) -> None:
        cfg = load_node_config(_write(tmp_path / "node.toml", "[agent]\nheartbeat_interval_seconds = 30\n"))
        assert cfg.agent.heartbeat_interval_seconds == 30.0
        assert isinstance(cfg.agent.heartbeat_interval_seconds, float)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        """A typo'd tuning key must not be silently ignored."""
        with pytest.raises(NodeConfigError, match="未知字段"):
            load_node_config(_write(tmp_path / "node.toml", "[agent]\nreconect_max_seconds = 5\n"))

    def test_non_numeric_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(NodeConfigError, match="必须是数字"):
            load_node_config(_write(tmp_path / "node.toml", '[agent]\nauth_retry_seconds = "soon"\n'))

    def test_bool_rejected(self, tmp_path: Path) -> None:
        """bool is an int subclass — must not pass the numeric check."""
        with pytest.raises(NodeConfigError, match="必须是数字"):
            load_node_config(_write(tmp_path / "node.toml", "[agent]\nauth_retry_seconds = true\n"))

    def test_non_positive_rejected(self, tmp_path: Path) -> None:
        """Zero would busy-loop the reconnect ramp."""
        with pytest.raises(NodeConfigError, match="必须大于 0"):
            load_node_config(_write(tmp_path / "node.toml", "[agent]\nreconnect_initial_seconds = 0\n"))

    def test_block_not_dict(self, tmp_path: Path) -> None:
        with pytest.raises(NodeConfigError, match="agent"):
            load_node_config(_write(tmp_path / "node.toml", 'agent = "bogus"\n'))

    def test_save_omits_defaults(self, tmp_path: Path) -> None:
        """`node init` and self-registration must keep producing a minimal file."""
        path = tmp_path / "node.toml"
        save_node_config(path, NodeConfig(server="s", node_id="x", token="y"))
        assert "[agent]" not in path.read_text(encoding="utf-8")

    def test_save_writes_non_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        save_node_config(
            path,
            NodeConfig(server="s", node_id="x", token="y", agent=AgentOptions(reconcile_interval_seconds=42.0)),
        )
        assert load_node_config(path).agent.reconcile_interval_seconds == 42.0

    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        opts = AgentOptions(
            reconnect_initial_seconds=2.0,
            reconnect_max_seconds=30.0,
            auth_retry_seconds=120.0,
            reconcile_interval_seconds=300.0,
            heartbeat_interval_seconds=15.0,
        )
        save_node_config(path, NodeConfig(server="s", node_id="x", token="y", agent=opts))
        assert load_node_config(path).agent == opts


class TestReloadPolicy:
    def test_defaults_to_auto(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        path.write_text('server = "http://x"\nnode_id = "n"\ntoken = "t"\n')
        assert load_node_config(path).reload_policy == "auto"

    @pytest.mark.parametrize("policy", ["auto", "never"])
    def test_accepts_valid(self, tmp_path: Path, policy: str) -> None:
        path = tmp_path / "node.toml"
        path.write_text(f'server = "http://x"\nnode_id = "n"\ntoken = "t"\n\n[apply]\nreload = "{policy}"\n')
        assert load_node_config(path).reload_policy == policy

    def test_rejects_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        path.write_text('server = "http://x"\nnode_id = "n"\ntoken = "t"\n\n[apply]\nreload = "sometimes"\n')
        with pytest.raises(NodeConfigError, match="reload"):
            load_node_config(path)

    def test_round_trips_through_save(self, tmp_path: Path) -> None:
        path = tmp_path / "node.toml"
        save_node_config(path, NodeConfig(server="http://x", node_id="n", token="t", reload_policy="never"))
        assert load_node_config(path).reload_policy == "never"

    def test_auto_is_not_written(self, tmp_path: Path) -> None:
        """默认值不写盘,保持 node init / 自注册产出的文件与以前一样精简。"""
        path = tmp_path / "node.toml"
        save_node_config(path, NodeConfig(server="http://x", node_id="n", token="t"))
        assert "reload" not in path.read_text()
