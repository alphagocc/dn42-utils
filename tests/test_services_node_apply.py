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

    def test_nm_peer(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer(backend="nm")])
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg)
        nm_dir = Path(payload["paths"]["nm_dir"])
        assert (nm_dir / "dn42_1234.nmconnection").exists()


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

    def test_nm_backend_stale_cleanup(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload_a = _make_payload(tmp_path, bgp=[_bgp_peer(backend="nm")])
        _seed_cache(cfg.cache_db_path, payload_a)
        apply(node_config=cfg)
        nm_dir = Path(payload_a["paths"]["nm_dir"])
        assert (nm_dir / "dn42_1234.nmconnection").exists()
        payload_b = _make_payload(tmp_path, bgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        apply(node_config=cfg)
        assert not (nm_dir / "dn42_1234.nmconnection").exists()


class TestSummary:
    def test_summary_counts(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = apply(node_config=cfg)
        text = apply_summary(result)
        assert "create=" in text
        assert result.revision in text
