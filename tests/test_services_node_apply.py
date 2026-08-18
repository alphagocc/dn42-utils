from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dn42ctl.node_config import NodeConfig
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.node_apply import apply, apply_diff_text, apply_summary

NODE_ID = "node-1"


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _seed_cache(cache_path: Path, payload: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cache_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cached_desired (
            id INTEGER PRIMARY KEY CHECK (id=1),
            revision TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO cached_desired(id,revision,payload_json,fetched_at) VALUES (1,?,?,?)",
        (payload["revision"], json.dumps(payload), _now_iso()),
    )
    conn.commit()
    conn.close()


def _make_payload(tmp_path: Path, bgp: list[dict] | None = None, ibgp: list[dict] | None = None) -> dict:
    paths = {
        "bird_conf_path": str(tmp_path / "bird/bird.conf"),
        "peers_dir": str(tmp_path / "bird/peers"),
        "babel_conf_path": str(tmp_path / "bird/babel.conf"),
        "networkd_dir": str(tmp_path / "networkd"),
        "nm_dir": str(tmp_path / "nm"),
    }
    return {
        "node_id": NODE_ID,
        "revision": "rev-1",
        "generated_at": _now_iso(),
        "bgp_peers": bgp or [],
        "ibgp_peers": ibgp or [],
        "paths": paths,
    }


def _bgp_peer(*, backend: str = "networkd") -> dict:
    return {
        "peer_asn": 4242421234,
        "ifname": "dn42_1234",
        "wg_private_key": "PRIV",
        "wg_public_key": "PUB",
        "peer_public_key": "PEERPUB",
        "endpoint": "peer.example:51820",
        "local_lla": "fe80::1",
        "peer_lla": "fe80::2",
        "listen_port": 21234,
        "allowed_ips": ["fe80::/64", "fd00::/8"],
        "net_backend": backend,
    }


def _ibgp_peer(*, has_wg: bool = True, backend: str = "networkd") -> dict:
    return {
        "name": "alpha",
        "ifname": "wg_alpha",
        "wg_private_key": "PRIV",
        "wg_public_key": "PUB",
        "peer_public_key": "PEERPUB",
        "endpoint": "alpha.example:51820",
        "local_lla": "fe80::10",
        "peer_lla": "fe80::20",
        "peer_ip": "fd42:1::1",
        "has_wg": has_wg,
        "listen_port": 31234,
        "allowed_ips": ["::/0"],
        "net_backend": backend,
        "babel_rxcost": 96,
        "babel_type": "tunnel",
    }


def _cfg(tmp_path: Path) -> NodeConfig:
    return NodeConfig(
        server="http://x",
        node_id=NODE_ID,
        token="t",
        cache_db_path=tmp_path / "node-cache.sqlite3",
    )


class TestNoCache:
    def test_apply_without_cache_errors(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        with pytest.raises(Dn42CtlError, match="缓存"):
            apply(node_config=cfg)


class TestEmpty:
    def test_empty_renders_babel_only(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        _seed_cache(cfg.cache_db_path, payload)
        result = apply(node_config=cfg)
        # babel.conf should exist (only file rendered when no peers)
        babel_path = Path(payload["paths"]["babel_conf_path"])
        assert babel_path.exists()
        assert any(d.path == babel_path for d in result.diffs)


class TestBgpPeer:
    def test_networkd_peer(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        peers_dir = Path(payload["paths"]["peers_dir"])
        networkd_dir = Path(payload["paths"]["networkd_dir"])
        assert (peers_dir / "dn42_1234.conf").exists()
        assert (networkd_dir / "dn42_1234.netdev").exists()
        assert (networkd_dir / "dn42_1234.network").exists()
        # netdev contains private key
        netdev = (networkd_dir / "dn42_1234.netdev").read_text()
        assert "PRIV" in netdev


class TestIbgpPeer:
    def test_with_wg(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, ibgp=[_ibgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        peers_dir = Path(payload["paths"]["peers_dir"])
        assert (peers_dir / "ibgp_alpha.conf").exists()
        babel_path = Path(payload["paths"]["babel_conf_path"])
        assert babel_path.exists()
        babel = babel_path.read_text()
        assert "wg_alpha" in babel
        assert "96" in babel  # rxcost

    def test_no_wg_skips_netdev(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, ibgp=[_ibgp_peer(has_wg=False)])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        # no netdev/.network/.nmconnection should exist
        networkd_dir = Path(payload["paths"]["networkd_dir"])
        assert not (networkd_dir / "wg_alpha.netdev").exists()
        # babel should not include this peer
        babel = Path(payload["paths"]["babel_conf_path"]).read_text()
        assert "wg_alpha" not in babel


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = apply(node_config=cfg, dry_run=True)
        assert result.dry_run is True
        peers_dir = Path(payload["paths"]["peers_dir"])
        assert not (peers_dir / "dn42_1234.conf").exists()
        # diff should mark create
        actions = {d.action for d in result.diffs}
        assert "create" in actions
        text = apply_diff_text(result)
        assert "create=" in text or "新文件" in text


class TestUpdate:
    def test_unchanged_then_modified(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        # Second apply: everything unchanged.
        result = apply(node_config=cfg)
        actions = {d.action for d in result.diffs}
        assert "unchanged" in actions

        # Modify peer and rerun -> update action.
        modified = _bgp_peer()
        modified["endpoint"] = "new.example:51820"
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[modified]))
        result2 = apply(node_config=cfg)
        actions2 = {d.action for d in result2.diffs}
        assert "update" in actions2


class TestApplyOverrides:
    def test_node_toml_overrides_peers_dir(self, tmp_path: Path) -> None:
        custom_peers = tmp_path / "custom-peers"
        cfg = NodeConfig(
            server="http://x",
            node_id=NODE_ID,
            token="t",
            apply_overrides={"peers_dir": str(custom_peers)},
            cache_db_path=tmp_path / "node-cache.sqlite3",
        )
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        assert (custom_peers / "dn42_1234.conf").exists()
        # Original peers_dir from payload should NOT have been used.
        orig_peers = Path(payload["paths"]["peers_dir"])
        assert not (orig_peers / "dn42_1234.conf").exists()


class TestAtomicWrite:
    def test_no_tmp_files_left(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        peers_dir = Path(payload["paths"]["peers_dir"])
        leftovers = list(peers_dir.glob(".dn42_1234.conf.*"))
        assert leftovers == []


class TestStaleDeletion:
    def test_deletes_files_no_longer_in_desired_state(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        # First apply with one BGP peer.
        payload_a = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload_a)
        apply(node_config=cfg)
        peers_dir = Path(payload_a["paths"]["peers_dir"])
        networkd_dir = Path(payload_a["paths"]["networkd_dir"])
        assert (peers_dir / "dn42_1234.conf").exists()
        assert (networkd_dir / "dn42_1234.netdev").exists()

        # Second apply with the peer removed entirely.
        payload_b = _make_payload(tmp_path, bgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        result = apply(node_config=cfg)
        assert not (peers_dir / "dn42_1234.conf").exists()
        assert not (networkd_dir / "dn42_1234.netdev").exists()
        assert not (networkd_dir / "dn42_1234.network").exists()
        # Result should record the deletions.
        actions = {d.action for d in result.diffs}
        assert "delete" in actions

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload_a = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload_a)
        apply(node_config=cfg)
        payload_b = _make_payload(tmp_path, bgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        result = apply(node_config=cfg, dry_run=True)
        peers_dir = Path(payload_a["paths"]["peers_dir"])
        # Files still on disk.
        assert (peers_dir / "dn42_1234.conf").exists()
        # But diff records pending deletes.
        assert any(d.action == "delete" for d in result.diffs)

    def test_does_not_delete_unrelated_files(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        # Drop a user-written file with non-dn42ctl naming.
        peers_dir = Path(payload["paths"]["peers_dir"])
        custom = peers_dir / "mycustom.conf"
        custom.write_text("custom peer config\n")
        # Now re-apply; mycustom.conf must not be touched.
        apply(node_config=cfg)
        assert custom.exists()

    def test_does_not_delete_files_outside_managed_dirs(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir(exist_ok=True)
        marker = unrelated / "dn42_9999.netdev"
        marker.write_text("not managed\n")
        apply(node_config=cfg)
        assert marker.exists()

    def test_ibgp_removal_cleans_files(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload_a = _make_payload(tmp_path, ibgp=[_ibgp_peer()])
        _seed_cache(cfg.cache_db_path, payload_a)
        apply(node_config=cfg)
        peers_dir = Path(payload_a["paths"]["peers_dir"])
        networkd_dir = Path(payload_a["paths"]["networkd_dir"])
        assert (peers_dir / "ibgp_alpha.conf").exists()
        assert (networkd_dir / "wg_alpha.netdev").exists()

        payload_b = _make_payload(tmp_path, ibgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        apply(node_config=cfg)
        assert not (peers_dir / "ibgp_alpha.conf").exists()
        assert not (networkd_dir / "wg_alpha.netdev").exists()


class TestSummary:
    def test_summary_counts(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = apply(node_config=cfg)
        text = apply_summary(result)
        assert "create=" in text
        assert result.revision in text


# --- 节点自身地址下发 (desired state 的 node 块) ---


def _app_config_toml(tmp_path: Path, *, own_ipv6: str = "fd42:4242:1234::1", router_id: str = "172.23.0.1") -> Path:
    """写一份最小可用的本机 config.toml。"""
    from dn42ctl.config import AppConfig, save_config

    path = tmp_path / "etc" / "config.toml"
    save_config(
        path,
        AppConfig(
            node_id=NODE_ID,
            own_asn=4242421234,
            router_id=router_id,
            own_ipv6=own_ipv6,
            ownnet_v6="fd42:4242:1234::/48",
            ownnetset_v6="[fd42:4242:1234::/48+]",
            bird_conf_path=str(tmp_path / "bird/bird.conf"),
            bird_peers_dir=str(tmp_path / "bird/peers"),
            bird_babel_conf_path=str(tmp_path / "bird/babel.conf"),
            bird_roa_v6_conf_path=str(tmp_path / "bird/roa_dn42_v6.conf"),
            networkd_dir=str(tmp_path / "networkd"),
            nm_system_connections_dir=str(tmp_path / "nm"),
            dummy_backend="networkd",
        ),
    )
    return path


def _cfg_with_config_path(tmp_path: Path, config_path: Path) -> NodeConfig:
    return NodeConfig(
        server="http://x",
        node_id=NODE_ID,
        token="t",
        cache_db_path=tmp_path / "node-cache.sqlite3",
        apply_overrides={"config_path": str(config_path)},
    )


class TestNodeBlockCompatibility:
    """兼容铰链:没有 node 块时,写入的文件集与本特性引入之前逐字节一致。"""

    def test_no_node_block_touches_nothing_extra(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()], ibgp=[_ibgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = apply(node_config=cfg)

        written = {p.name for p in result.written}
        assert written == {
            "dn42_1234.conf",
            "dn42_1234.netdev",
            "dn42_1234.network",
            "ibgp_alpha.conf",
            "wg_alpha.netdev",
            "wg_alpha.network",
            "babel.conf",
        }
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert not (tmp_path / "networkd" / "dn42-dummy.network").exists()
        assert result.warnings == []

    def test_empty_node_block_is_same_as_absent(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        payload["node"] = {}
        _seed_cache(cfg.cache_db_path, payload)
        result = apply(node_config=cfg)
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert result.warnings == []


class TestNodeBlockApplied:
    def test_renders_bird_conf_and_dummy(self, tmp_path: Path) -> None:
        config_path = _app_config_toml(tmp_path)
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9", "router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        assert result.warnings == []

        bird_conf = (tmp_path / "bird" / "bird.conf").read_text()
        assert "fd42:4242:1234::9" in bird_conf
        assert "172.23.0.9" in bird_conf

        dummy = (tmp_path / "networkd" / "dn42-dummy.network").read_text()
        assert "fd42:4242:1234::9/128" in dummy
        assert (tmp_path / "networkd" / "dn42-dummy.netdev").exists()

    def test_rewrites_config_toml(self, tmp_path: Path) -> None:
        from dn42ctl.config import load_config

        config_path = _app_config_toml(tmp_path)
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        apply(node_config=cfg)
        assert load_config(config_path).own_ipv6 == "fd42:4242:1234::9"
        # 未下发的字段保持本地值
        assert load_config(config_path).router_id == "172.23.0.1"

    def test_unchanged_values_do_not_rewrite_config_toml(self, tmp_path: Path) -> None:
        """先比较、有差异才写:save_config 会丢注释与未知键,常规路径不该碰这个文件。"""
        config_path = _app_config_toml(tmp_path, own_ipv6="fd42:4242:1234::1")
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::1"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        assert config_path not in result.written

    def test_only_non_null_keys_applied(self, tmp_path: Path) -> None:
        from dn42ctl.config import load_config

        config_path = _app_config_toml(tmp_path)
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)

        apply(node_config=cfg)
        merged = load_config(config_path)
        assert merged.router_id == "172.23.0.9"
        assert merged.own_ipv6 == "fd42:4242:1234::1"

    def test_missing_config_toml_warns_and_skips(self, tmp_path: Path) -> None:
        """纯 spoke 可能从没有过 config.toml。bird.conf 还需要 own_asn 等 AS 级字段,
        缺了就渲染不出来 —— 只能跳过并告警,绝不伪造。"""
        cfg = _cfg_with_config_path(tmp_path, tmp_path / "etc" / "nope.toml")
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        assert len(result.warnings) == 1
        assert "config.toml 不可用" in result.warnings[0]
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert not (tmp_path / "networkd" / "dn42-dummy.network").exists()

    def test_broken_config_toml_warns_and_skips(self, tmp_path: Path) -> None:
        bad = tmp_path / "etc" / "config.toml"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("this is not valid toml {{{")
        cfg = _cfg_with_config_path(tmp_path, bad)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        assert len(result.warnings) == 1
        assert not (tmp_path / "bird" / "bird.conf").exists()

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        config_path = _app_config_toml(tmp_path)
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, dry_run=True)
        assert result.written == []
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert "fd42:4242:1234::9" not in config_path.read_text()
        assert any(d.path == tmp_path / "bird" / "bird.conf" for d in result.diffs)


class TestEmptyPeerIpIsSkipped:
    def test_missing_peer_ip_does_not_fail_whole_apply(self, tmp_path: Path) -> None:
        """空 peer_ip 只跳过这一个 bird 文件,不能让整个 apply 炸掉。"""
        cfg = _cfg(tmp_path)
        peer = _ibgp_peer()
        peer["peer_ip"] = ""
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()], ibgp=[peer])
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        names = {p.name for p in result.written}
        assert "ibgp_alpha.conf" not in names
        # 其它文件照常写
        assert {"wg_alpha.netdev", "dn42_1234.conf", "babel.conf"} <= names


class TestReload:
    @staticmethod
    def _recording_runner(seen: list[list[str]]):
        from dn42ctl.services.reload import ReloadAction

        def runner(cmd: list[str]) -> ReloadAction:
            seen.append(cmd)
            return ReloadAction(cmd=cmd, ok=True, output="")

        return runner

    def test_runs_both_when_both_dirs_touched(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        seen: list[list[str]] = []

        apply(node_config=cfg, runner=self._recording_runner(seen))
        assert seen == [["networkctl", "reload"], ["birdc", "configure"]]

    def test_second_apply_with_no_changes_runs_nothing(self, tmp_path: Path) -> None:
        """agent 每 900 秒 reconcile 一次,无变更就 reload 会让每节点每天空跑 96 次。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg, runner=self._recording_runner([]))

        seen: list[list[str]] = []
        result = apply(node_config=cfg, runner=self._recording_runner(seen))
        assert seen == []
        assert result.reloads == []

    def test_dry_run_never_reloads(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        seen: list[list[str]] = []
        apply(node_config=cfg, dry_run=True, runner=self._recording_runner(seen))
        assert seen == []

    def test_no_reload_flag(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        seen: list[list[str]] = []
        apply(node_config=cfg, no_reload=True, runner=self._recording_runner(seen))
        assert seen == []

    def test_reload_policy_never(self, tmp_path: Path) -> None:
        cfg = NodeConfig(
            server="http://x",
            node_id=NODE_ID,
            token="t",
            cache_db_path=tmp_path / "node-cache.sqlite3",
            reload_policy="never",
        )
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        seen: list[list[str]] = []
        apply(node_config=cfg, runner=self._recording_runner(seen))
        assert seen == []

    def test_failure_becomes_warning_not_exception(self, tmp_path: Path) -> None:
        from dn42ctl.services.reload import ReloadAction

        def failing(cmd: list[str]) -> ReloadAction:
            return ReloadAction(cmd=cmd, ok=False, error="boom")

        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, runner=failing)
        assert len(result.warnings) == 2
        assert all("失败" in w for w in result.warnings)
        assert [a.ok for a in result.reloads] == [False, False]


class TestNodeBlockPathsAndBackend:
    def test_bird_conf_uses_resolved_paths_not_config_toml(self, tmp_path: Path) -> None:
        """peer 文件与 babel.conf 是按解析后的路径写出去的,bird.conf 必须 include 同一处。
        用 config.toml 里的旧路径会让 bird 去 include 一个空目录。"""
        from dn42ctl.config import AppConfig, save_config

        stale = tmp_path / "stale"
        config_path = tmp_path / "etc" / "config.toml"
        save_config(
            config_path,
            AppConfig(
                node_id=NODE_ID,
                own_asn=4242421234,
                router_id="172.23.0.1",
                own_ipv6="fd42:4242:1234::1",
                ownnet_v6="fd42:4242:1234::/48",
                ownnetset_v6="[fd42:4242:1234::/48+]",
                bird_conf_path=str(stale / "bird.conf"),
                bird_peers_dir=str(stale / "peers"),
                bird_babel_conf_path=str(stale / "babel.conf"),
                bird_roa_v6_conf_path=str(stale / "roa.conf"),
                networkd_dir=str(stale / "networkd"),
                nm_system_connections_dir=str(stale / "nm"),
                dummy_backend="networkd",
            ),
        )
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)  # paths 指向 tmp_path/bird/...
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        apply(node_config=cfg)
        bird_conf = (tmp_path / "bird" / "bird.conf").read_text()
        assert str(tmp_path / "bird" / "peers") in bird_conf
        assert str(tmp_path / "bird" / "babel.conf") in bird_conf
        assert str(stale / "peers") not in bird_conf
        assert str(stale / "babel.conf") not in bird_conf

    def test_nm_dummy_backend_skips_networkd_dummy_files(self, tmp_path: Path) -> None:
        """dummy_backend=nm 时写 networkd 的 dn42-dummy.* 会造出与 NM 冲突的配置。"""
        from dn42ctl.config import AppConfig, save_config

        config_path = tmp_path / "etc" / "config.toml"
        save_config(
            config_path,
            AppConfig(
                node_id=NODE_ID,
                own_asn=4242421234,
                router_id="172.23.0.1",
                own_ipv6="fd42:4242:1234::1",
                ownnet_v6="fd42:4242:1234::/48",
                ownnetset_v6="[fd42:4242:1234::/48+]",
                bird_conf_path=str(tmp_path / "bird/bird.conf"),
                bird_peers_dir=str(tmp_path / "bird/peers"),
                bird_babel_conf_path=str(tmp_path / "bird/babel.conf"),
                bird_roa_v6_conf_path=str(tmp_path / "bird/roa.conf"),
                networkd_dir=str(tmp_path / "networkd"),
                nm_system_connections_dir=str(tmp_path / "nm"),
                dummy_backend="nm",
            ),
        )
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        assert not (tmp_path / "networkd" / "dn42-dummy.netdev").exists()
        assert not (tmp_path / "networkd" / "dn42-dummy.network").exists()
        assert any("NetworkManager" in w for w in result.warnings)
        # config.toml 与 bird.conf 仍然照常更新
        assert "fd42:4242:1234::9" in (tmp_path / "bird" / "bird.conf").read_text()

    def test_networkd_backend_still_writes_dummy(self, tmp_path: Path) -> None:
        config_path = _app_config_toml(tmp_path)
        cfg = _cfg_with_config_path(tmp_path, config_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg)
        assert (tmp_path / "networkd" / "dn42-dummy.network").exists()
        assert result.warnings == []
