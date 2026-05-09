from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.services.core import (
    Dn42CtlError,
    delete_files_and_collect_status,
    ensure_dir,
    normalize_net_backend,
    open_db_and_ensure_node,
    parse_allowed_ips_json,
    peer_files_for_backend,
    pick_unused_port,
    sanitize_name,
    write_text,
)


class TestWriteText:
    def test_write(self, tmp_path: Path) -> None:
        p = tmp_path / "test.txt"
        write_text(p, "hello")
        assert p.read_text() == "hello"

    def test_creates_parents(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "test.txt"
        write_text(p, "nested")
        assert p.read_text() == "nested"

    def test_permission_error(self, tmp_path: Path) -> None:
        p = tmp_path / "test.txt"
        p.write_text("original")
        p.chmod(0o000)
        try:
            with pytest.raises(Dn42CtlError, match="权限不足"):
                write_text(p, "new")
        finally:
            p.chmod(0o644)


class TestEnsureDir:
    def test_creates(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b"
        ensure_dir(d)
        assert d.is_dir()

    def test_existing(self, tmp_path: Path) -> None:
        ensure_dir(tmp_path)


class TestPickUnusedPort:
    def test_returns_in_range(self) -> None:
        port = pick_unused_port(set())
        assert 20000 <= port <= 65535

    def test_avoids_used(self) -> None:
        used = {20000, 20001, 20002}
        port = pick_unused_port(used)
        assert port not in used
        assert 20000 <= port <= 65535

    def test_exhaustion_raises(self) -> None:
        used = set(range(20000, 65536))
        with pytest.raises(Dn42CtlError, match="无法自动选择"):
            pick_unused_port(used)


class TestSanitizeName:
    def test_basic(self) -> None:
        assert sanitize_name("MyNode") == "mynode"

    def test_special_chars(self) -> None:
        assert sanitize_name("my-node!@#") == "my_node"

    def test_empty_raises(self) -> None:
        with pytest.raises(Dn42CtlError, match="不能为空"):
            sanitize_name("   ")

    def test_all_special(self) -> None:
        with pytest.raises(Dn42CtlError, match="不能为空"):
            sanitize_name("!@#$%")


class TestNormalizeNetBackend:
    def test_networkd(self) -> None:
        assert normalize_net_backend("networkd") == "networkd"

    def test_nm(self) -> None:
        assert normalize_net_backend("nm") == "nm"

    def test_invalid_raises(self) -> None:
        with pytest.raises(Dn42CtlError):
            normalize_net_backend("invalid")


class TestParseAllowedIpsJson:
    def test_valid_json(self) -> None:
        assert parse_allowed_ips_json('["fe80::/64", "fd00::/8"]') == [
            "fe80::/64",
            "fd00::/8",
        ]

    def test_none_returns_default(self) -> None:
        result = parse_allowed_ips_json(None)
        assert result == ["fe80::/64", "fd00::/8"]

    def test_empty_returns_default(self) -> None:
        result = parse_allowed_ips_json("")
        assert result == ["fe80::/64", "fd00::/8"]

    def test_malformed_returns_default(self) -> None:
        result = parse_allowed_ips_json("{bad}")
        assert result == ["fe80::/64", "fd00::/8"]

    def test_non_list_returns_default(self) -> None:
        result = parse_allowed_ips_json('"string"')
        assert result == ["fe80::/64", "fd00::/8"]


class TestPeerFilesForBackend:
    def test_bgp_networkd(self, sample_config) -> None:
        files = peer_files_for_backend(
            config=sample_config,
            ifname="dn42_1234",
            net_backend="networkd",
            kind="bgp",
        )
        paths = [str(f) for f in files]
        assert any("dn42_1234.conf" in p for p in paths)
        assert any("dn42_1234.netdev" in p for p in paths)
        assert any("dn42_1234.network" in p for p in paths)

    def test_ibgp_nm(self, sample_config) -> None:
        files = peer_files_for_backend(
            config=sample_config,
            ifname="wg_mynode",
            net_backend="nm",
            kind="ibgp",
            ibgp_name="mynode",
        )
        paths = [str(f) for f in files]
        assert any("ibgp_mynode.conf" in p for p in paths)
        assert any("wg_mynode.nmconnection" in p for p in paths)
        assert any("babel.conf" in p for p in paths)


class TestDeleteFilesAndCollectStatus:
    def test_deletes_existing(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello")
        deleted, missing = delete_files_and_collect_status([f])
        assert str(f) in deleted
        assert not f.exists()

    def test_missing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.txt"
        deleted, missing = delete_files_and_collect_status([f])
        assert str(f) in missing


class TestOpenDbAndEnsureNode:
    def test_creates_db(self, db_path: Path) -> None:
        db = open_db_and_ensure_node(db_path, "test-node")
        row = db._conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_id='test-node'"
        ).fetchone()
        assert row[0] == 1
        db.close()
