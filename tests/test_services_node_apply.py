from __future__ import annotations

import dataclasses
import errno
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dn42ctl import file_policy
from dn42ctl.config import load_config
from dn42ctl.node_config import NodeConfig
from dn42ctl.services import node_apply
from dn42ctl.services.core import Dn42CtlError, write_bird_bgp_peer, write_net_backend_files
from dn42ctl.services.node_apply import ApplyResult, apply, apply_diff_text, apply_summary

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


def _paths(tmp_path: Path) -> dict[str, str]:
    """Where a test expects apply to write, all under tmp_path.

    `_local_config` writes exactly these into the config.toml that apply reads, so
    the expectations and the input cannot drift apart. Every test must pass that
    config.toml as `config_path=`; without it apply falls back to the built-in
    defaults and would write to the real /etc.
    """
    return {
        "bird_conf_path": str(tmp_path / "bird/bird.conf"),
        "peers_dir": str(tmp_path / "bird/peers"),
        "babel_conf_path": str(tmp_path / "bird/babel.conf"),
        "bird_extra_conf_path": str(tmp_path / "bird/extra.conf"),
        "networkd_dir": str(tmp_path / "networkd"),
        "nm_dir": str(tmp_path / "nm"),
    }


def _local_config(
    tmp_path: Path,
    *,
    own_ipv6: str = "fd42:4242:1234::1",
    router_id: str = "172.23.0.1",
    bird_conf_path: Path | None = None,
    dummy_backend: str = "networkd",
) -> Path:
    """写一份最小可用的本机 config.toml,并返回它的位置。"""
    from dn42ctl.config import AppConfig, save_config

    locations = _paths(tmp_path)
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
            bird_conf_path=str(bird_conf_path) if bird_conf_path else locations["bird_conf_path"],
            bird_peers_dir=locations["peers_dir"],
            bird_babel_conf_path=locations["babel_conf_path"],
            bird_roa_v6_conf_path=str(tmp_path / "bird/roa_dn42_v6.conf"),
            bird_extra_conf_path=locations["bird_extra_conf_path"],
            networkd_dir=locations["networkd_dir"],
            nm_system_connections_dir=locations["nm_dir"],
            dummy_backend=dummy_backend,
        ),
    )
    return path


def _apply(cfg: NodeConfig, tmp_path: Path, **kwargs: Any) -> ApplyResult:
    """apply with this machine's config.toml, which is what decides the locations."""
    return apply(node_config=cfg, config_path=_local_config(tmp_path), **kwargs)


@pytest.fixture(autouse=True)
def _defaults_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把内置默认值改指 tmp_path。

    config.toml 缺失或损坏时 apply 会落到默认值,那是 `/etc` 下的真实位置,
    一次单元测试不该读写它们。
    """
    locations = _paths(tmp_path)
    monkeypatch.setattr(
        node_apply,
        "_PATH_SOURCES",
        (
            ("bird_peers_dir", "bird_peers_dir", Path(locations["peers_dir"])),
            ("babel_conf_path", "bird_babel_conf_path", Path(locations["babel_conf_path"])),
            ("networkd_dir", "networkd_dir", Path(locations["networkd_dir"])),
            ("nm_dir", "nm_system_connections_dir", Path(locations["nm_dir"])),
            ("bird_conf_path", "bird_conf_path", Path(locations["bird_conf_path"])),
            ("bird_extra_conf_path", "bird_extra_conf_path", Path(locations["bird_extra_conf_path"])),
        ),
    )


def _make_payload(tmp_path: Path, bgp: list[dict] | None = None, ibgp: list[dict] | None = None) -> dict:
    return {
        "node_id": NODE_ID,
        "revision": "rev-1",
        "generated_at": _now_iso(),
        "bgp_peers": bgp or [],
        "ibgp_peers": ibgp or [],
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
            _apply(cfg, tmp_path)


class TestEmpty:
    def test_empty_renders_babel_only(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        _seed_cache(cfg.cache_db_path, payload)
        result = _apply(cfg, tmp_path)
        babel_path = Path(_paths(tmp_path)["babel_conf_path"])
        assert babel_path.exists()
        assert any(d.path == babel_path for d in result.diffs)


class TestBgpPeer:
    def test_networkd_peer(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        networkd_dir = Path(_paths(tmp_path)["networkd_dir"])
        assert (peers_dir / "dn42_1234.conf").exists()
        assert (networkd_dir / "dn42_1234.netdev").exists()
        assert (networkd_dir / "dn42_1234.network").exists()
        netdev = (networkd_dir / "dn42_1234.netdev").read_text()
        assert "PRIV" in netdev


class TestIbgpPeer:
    def test_with_wg(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, ibgp=[_ibgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        assert (peers_dir / "ibgp_alpha.conf").exists()
        babel_path = Path(_paths(tmp_path)["babel_conf_path"])
        assert babel_path.exists()
        babel = babel_path.read_text()
        assert "wg_alpha" in babel
        assert "96" in babel  # rxcost

    def test_no_wg_skips_netdev(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, ibgp=[_ibgp_peer(has_wg=False)])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        networkd_dir = Path(_paths(tmp_path)["networkd_dir"])
        assert not (networkd_dir / "wg_alpha.netdev").exists()
        babel = Path(_paths(tmp_path)["babel_conf_path"]).read_text()
        assert "wg_alpha" not in babel


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = _apply(cfg, tmp_path, dry_run=True)
        assert result.dry_run is True
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        assert not (peers_dir / "dn42_1234.conf").exists()
        actions = {d.action for d in result.diffs}
        assert "create" in actions
        text = apply_diff_text(result)
        assert "create=" in text or "新文件" in text


class TestUpdate:
    def test_unchanged_then_modified(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        result = _apply(cfg, tmp_path)
        actions = {d.action for d in result.diffs}
        assert "unchanged" in actions

        modified = _bgp_peer()
        modified["endpoint"] = "new.example:51820"
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[modified]))
        result2 = _apply(cfg, tmp_path)
        actions2 = {d.action for d in result2.diffs}
        assert "update" in actions2


class TestLocationResolution:
    def test_config_toml_decides_where_files_go(self, tmp_path: Path) -> None:
        """写入位置的唯一来源是本机 config.toml,与 genconf 读的是同一处。"""
        cfg = _cfg(tmp_path)
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[_bgp_peer()]))
        _apply(cfg, tmp_path)

        assert (tmp_path / "bird" / "peers" / "dn42_1234.conf").exists()
        assert (tmp_path / "networkd" / "dn42_1234.netdev").exists()

    def test_distro_bird_conf_location(self, tmp_path: Path) -> None:
        """发行版默认布局把主配置放在 /etc/bird.conf,改 config.toml 的 bird_conf 即可。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9", "router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(
            node_config=cfg,
            config_path=_local_config(tmp_path, bird_conf_path=tmp_path / "bird.conf"),
        )
        assert (tmp_path / "bird.conf").exists()
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert result.warnings == []

    def test_missing_config_toml_falls_back_quietly(self, tmp_path: Path) -> None:
        """纯 spoke 只跑过 node init,没有 config.toml。落到内置默认值是正常行为,
        每 900 秒的 reconcile 不该为此各报一次。"""
        cfg = _cfg(tmp_path)
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[_bgp_peer()]))
        result = apply(node_config=cfg, config_path=tmp_path / "absent.toml")

        assert result.warnings == []
        assert (Path(_paths(tmp_path)["peers_dir"]) / "dn42_1234.conf").exists()

    def test_pushed_paths_are_ignored(self, tmp_path: Path) -> None:
        """旧中心仍会下发 paths;报文里的该键不再影响任何写入位置。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        payload["paths"] = {"peers_dir": str(tmp_path / "from-hub")}
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)

        assert not (tmp_path / "from-hub").exists()
        assert (Path(_paths(tmp_path)["peers_dir"]) / "dn42_1234.conf").exists()


class TestFileModes:
    def test_bird_files_are_readable_by_the_bird_user(self, tmp_path: Path) -> None:
        """bird.conf 权限为 0600 root 会让 birdc configure 失败。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()], ibgp=[_ibgp_peer()])
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9", "router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)

        locations = _paths(tmp_path)
        bird_files = [
            Path(locations["bird_conf_path"]),
            Path(locations["babel_conf_path"]),
            Path(locations["bird_extra_conf_path"]),
            Path(locations["peers_dir"]) / "dn42_1234.conf",
            Path(locations["peers_dir"]) / "ibgp_alpha.conf",
        ]
        for path in bird_files:
            assert path.stat().st_mode & 0o044 == 0o044, path

    def test_secrets_stay_private(self, tmp_path: Path) -> None:
        """私钥在 .netdev 里,config.toml 是 dn42ctl 自己的配置,两者都不放宽。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)
        config_path = _local_config(tmp_path)
        apply(node_config=cfg, config_path=config_path)

        netdev = Path(_paths(tmp_path)["networkd_dir"]) / "dn42_1234.netdev"
        assert "PRIV" in netdev.read_text()
        assert netdev.stat().st_mode & 0o007 == 0
        assert config_path.stat().st_mode & 0o077 == 0

    def test_networkd_files_are_readable_by_the_networkd_user(self, tmp_path: Path) -> None:
        """systemd-networkd 以 systemd-network 用户运行,读不到 root 独占的文件。

        它读不到时不会报错退出,而是逐个文件记一行 Permission denied 继续跑,接口
        因此建起来却拿不到地址,BGP 与 Babel 全部失去承载。
        """
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()], ibgp=[_ibgp_peer()])
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9", "router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)

        networkd_dir = Path(_paths(tmp_path)["networkd_dir"])
        for name in ("dn42_1234.network", "wg_alpha.network", "dn42-dummy.network"):
            assert (networkd_dir / name).stat().st_mode & 0o004 == 0o004, name
        # netdev 含私钥,networkd 靠属组读它,所以是组可读而非全局可读。
        for name in ("dn42_1234.netdev", "wg_alpha.netdev", "dn42-dummy.netdev"):
            assert (networkd_dir / name).stat().st_mode & 0o777 == 0o640, name

    def test_netdev_policy_names_the_networkd_group(self) -> None:
        """0640 本身不够:属组仍是 root 的话 networkd 依然读不到。"""
        assert file_policy.NETDEV.group == "systemd-network"

    def test_existing_extra_conf_gets_its_permissions_corrected(self, tmp_path: Path) -> None:
        """extra.conf 只在缺失时进入写入列表,已存在的那份权限仍归工具管。

        早先版本按 0600 创建过它,bird 打不开被 include 的文件会拒绝加载整份配置,
        而它的内容归运维,不能靠重写文件顺带修好权限。
        """
        cfg = _cfg(tmp_path)
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[_bgp_peer()]))
        extra = Path(_paths(tmp_path)["bird_extra_conf_path"])
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("protocol static custom { ipv6; }\n")
        extra.chmod(0o600)

        _apply(cfg, tmp_path)

        assert extra.stat().st_mode & 0o777 == 0o644
        assert extra.read_text() == "protocol static custom { ipv6; }\n"

    def test_dry_run_touches_no_permissions(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[_bgp_peer()]))
        extra = Path(_paths(tmp_path)["bird_extra_conf_path"])
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("protocol static custom { ipv6; }\n")
        extra.chmod(0o600)

        _apply(cfg, tmp_path, dry_run=True)

        assert extra.stat().st_mode & 0o777 == 0o600


class TestWritersAgree:
    """genconf 与 agent 写同一个文件时权限必须一致。

    两者曾各自声明过一份:agent 把 `.network` 写成 0600,networkd 重启后所有 wg 接口
    失去地址。权限现在只在 `dn42ctl/file_policy.py` 声明一次,这里守住它不再分叉。
    """

    def _genconf_networkd_dir(self, tmp_path: Path, config_path: Path) -> Path:
        out = tmp_path / "genconf-networkd"
        peer = _bgp_peer()
        write_net_backend_files(
            config=dataclasses.replace(load_config(config_path), networkd_dir=str(out)),
            node_id=NODE_ID,
            backend="networkd",
            ifname=peer["ifname"],
            private_key=peer["wg_private_key"],
            listen_port=peer["listen_port"],
            peer_public_key=peer["peer_public_key"],
            endpoint=peer["endpoint"],
            allowed_ips=peer["allowed_ips"],
            local_lla=peer["local_lla"],
            peer_lla=peer["peer_lla"],
            generated=[],
        )
        return out

    def _genconf_peers_dir(self, tmp_path: Path, config_path: Path) -> Path:
        out = tmp_path / "genconf-peers"
        peer = _bgp_peer()
        write_bird_bgp_peer(
            config=dataclasses.replace(load_config(config_path), bird_peers_dir=str(out)),
            ifname=peer["ifname"],
            peer_lla=peer["peer_lla"],
            peer_asn=peer["peer_asn"],
            generated=[],
        )
        return out

    def test_same_peer_files_get_the_same_modes(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        _seed_cache(cfg.cache_db_path, _make_payload(tmp_path, bgp=[_bgp_peer()]))
        config_path = _local_config(tmp_path)
        apply(node_config=cfg, config_path=config_path)

        pairs = [
            (Path(_paths(tmp_path)["networkd_dir"]), self._genconf_networkd_dir(tmp_path, config_path)),
            (Path(_paths(tmp_path)["peers_dir"]), self._genconf_peers_dir(tmp_path, config_path)),
        ]
        for agent_dir, genconf_dir in pairs:
            for genconf_file in sorted(genconf_dir.iterdir()):
                agent_file = agent_dir / genconf_file.name
                assert agent_file.stat().st_mode & 0o777 == genconf_file.stat().st_mode & 0o777, agent_file


class TestAtomicWrite:
    def test_no_tmp_files_left(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        leftovers = list(peers_dir.glob(".dn42_1234.conf.*"))
        assert leftovers == []


class TestWriteFallbackWhenRenameIsBlocked:
    """`ReadWritePaths=` 指向单个文件时该文件是挂载点:父目录只读让 mkstemp 得到
    EROFS,覆盖挂载点让 rename 得到 EBUSY。两种情况都只能原地覆写。"""

    @staticmethod
    def _apply_once(tmp_path: Path) -> tuple[NodeConfig, Path]:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        return cfg, Path(_paths(tmp_path)["peers_dir"]) / "dn42_1234.conf"

    def test_read_only_parent_falls_back_to_in_place(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg, peer_conf = self._apply_once(tmp_path)
        rendered = peer_conf.read_text()
        peer_conf.write_text("清空,让下一次 apply 判定为 update\n")

        def blocked(*_args: object, **_kwargs: object) -> tuple[int, str]:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(node_apply.tempfile, "mkstemp", blocked)
        _apply(cfg, tmp_path)

        assert peer_conf.read_text() == rendered

    def test_rename_onto_mount_point_falls_back_to_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg, peer_conf = self._apply_once(tmp_path)
        rendered = peer_conf.read_text()
        peer_conf.write_text("清空,让下一次 apply 判定为 update\n")

        def blocked(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        monkeypatch.setattr(node_apply.os, "replace", blocked)
        _apply(cfg, tmp_path)

        assert peer_conf.read_text() == rendered
        assert list(peer_conf.parent.glob(".dn42_1234.conf.*")) == []

    def test_missing_target_still_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """沙盒下无法在只读目录里新建文件,静默降级只会掩盖配置错误。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)

        def blocked(*_args: object, **_kwargs: object) -> tuple[int, str]:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(node_apply.tempfile, "mkstemp", blocked)
        with pytest.raises(OSError, match="Read-only"):
            _apply(cfg, tmp_path)


class TestStaleDeletion:
    def test_deletes_files_no_longer_in_desired_state(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload_a = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload_a)
        _apply(cfg, tmp_path)
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        networkd_dir = Path(_paths(tmp_path)["networkd_dir"])
        assert (peers_dir / "dn42_1234.conf").exists()
        assert (networkd_dir / "dn42_1234.netdev").exists()

        payload_b = _make_payload(tmp_path, bgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        result = _apply(cfg, tmp_path)
        assert not (peers_dir / "dn42_1234.conf").exists()
        assert not (networkd_dir / "dn42_1234.netdev").exists()
        assert not (networkd_dir / "dn42_1234.network").exists()
        actions = {d.action for d in result.diffs}
        assert "delete" in actions

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload_a = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload_a)
        _apply(cfg, tmp_path)
        payload_b = _make_payload(tmp_path, bgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        result = _apply(cfg, tmp_path, dry_run=True)
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        assert (peers_dir / "dn42_1234.conf").exists()
        assert any(d.action == "delete" for d in result.diffs)

    def test_does_not_delete_unrelated_files(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        # Drop a user-written file with non-dn42ctl naming.
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        custom = peers_dir / "mycustom.conf"
        custom.write_text("custom peer config\n")
        _apply(cfg, tmp_path)
        assert custom.exists()

    def test_does_not_delete_files_outside_managed_dirs(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path)
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir(exist_ok=True)
        marker = unrelated / "dn42_9999.netdev"
        marker.write_text("not managed\n")
        _apply(cfg, tmp_path)
        assert marker.exists()

    def test_bird_conf_parent_dir_is_never_scanned(self, tmp_path: Path) -> None:
        """主配置指向 /etc/bird.conf 时,它的父目录是 /etc,扫描那里会波及无关文件。"""
        local_config = _local_config(tmp_path, bird_conf_path=tmp_path / "bird.conf")
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9", "router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)
        apply(node_config=cfg, config_path=local_config)

        bystander = tmp_path / "dn42_bystander.conf"
        bystander.write_text("与 dn42ctl 无关\n")
        apply(node_config=cfg, config_path=local_config)
        assert bystander.exists()

    def test_ibgp_removal_cleans_files(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload_a = _make_payload(tmp_path, ibgp=[_ibgp_peer()])
        _seed_cache(cfg.cache_db_path, payload_a)
        _apply(cfg, tmp_path)
        peers_dir = Path(_paths(tmp_path)["peers_dir"])
        networkd_dir = Path(_paths(tmp_path)["networkd_dir"])
        assert (peers_dir / "ibgp_alpha.conf").exists()
        assert (networkd_dir / "wg_alpha.netdev").exists()

        payload_b = _make_payload(tmp_path, ibgp=[])
        _seed_cache(cfg.cache_db_path, payload_b)
        _apply(cfg, tmp_path)
        assert not (peers_dir / "ibgp_alpha.conf").exists()
        assert not (networkd_dir / "wg_alpha.netdev").exists()


class TestSummary:
    def test_summary_counts(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = _apply(cfg, tmp_path)
        text = apply_summary(result)
        assert "create=" in text
        assert result.revision in text


# --- 节点自身地址下发 (desired state 的 node 块) ---


class TestNodeBlockCompatibility:
    """兼容铰链:没有 node 块时,写入的文件集与本特性引入之前逐字节一致。"""

    def test_no_node_block_touches_nothing_extra(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()], ibgp=[_ibgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        result = _apply(cfg, tmp_path)

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
        result = _apply(cfg, tmp_path)
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert result.warnings == []


class TestNodeBlockApplied:
    def test_renders_bird_conf_and_dummy(self, tmp_path: Path) -> None:
        local_config = _local_config(tmp_path)
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9", "router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, config_path=local_config)
        assert result.warnings == []

        bird_conf = (tmp_path / "bird" / "bird.conf").read_text()
        assert "fd42:4242:1234::9" in bird_conf
        assert "172.23.0.9" in bird_conf

        dummy = (tmp_path / "networkd" / "dn42-dummy.network").read_text()
        assert "fd42:4242:1234::9/128" in dummy
        assert (tmp_path / "networkd" / "dn42-dummy.netdev").exists()

    def test_rewrites_config_toml(self, tmp_path: Path) -> None:
        from dn42ctl.config import load_config

        local_config = _local_config(tmp_path)
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        apply(node_config=cfg, config_path=local_config)
        assert load_config(local_config).own_ipv6 == "fd42:4242:1234::9"
        # 未下发的字段保持本地值
        assert load_config(local_config).router_id == "172.23.0.1"

    def test_unchanged_values_do_not_rewrite_config_toml(self, tmp_path: Path) -> None:
        """先比较、有差异才写:save_config 会丢注释与未知键,常规路径不该碰这个文件。"""
        local_config = _local_config(tmp_path, own_ipv6="fd42:4242:1234::1")
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::1"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, config_path=local_config)
        assert local_config not in result.written

    def test_only_non_null_keys_applied(self, tmp_path: Path) -> None:
        from dn42ctl.config import load_config

        local_config = _local_config(tmp_path)
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"router_id": "172.23.0.9"}
        _seed_cache(cfg.cache_db_path, payload)

        apply(node_config=cfg, config_path=local_config)
        merged = load_config(local_config)
        assert merged.router_id == "172.23.0.9"
        assert merged.own_ipv6 == "fd42:4242:1234::1"

    def test_missing_config_toml_warns_and_skips(self, tmp_path: Path) -> None:
        """纯 spoke 可能从没有过 config.toml。bird.conf 还需要 own_asn 等 AS 级字段,
        缺了就渲染不出来,只能跳过并告警,绝不伪造。"""
        cfg = _cfg(tmp_path)
        local_config = tmp_path / "etc" / "nope.toml"
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, config_path=local_config)
        assert len(result.warnings) == 1
        assert "config.toml 不可用" in result.warnings[0]
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert not (tmp_path / "networkd" / "dn42-dummy.network").exists()

    def test_broken_config_toml_warns_and_skips(self, tmp_path: Path) -> None:
        bad = tmp_path / "etc" / "config.toml"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("this is not valid toml {{{")
        cfg = _cfg(tmp_path)
        local_config = bad
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, config_path=local_config)
        assert len(result.warnings) == 1
        assert not (tmp_path / "bird" / "bird.conf").exists()

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        local_config = _local_config(tmp_path)
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, config_path=local_config, dry_run=True)
        assert result.written == []
        assert not (tmp_path / "bird" / "bird.conf").exists()
        assert "fd42:4242:1234::9" not in local_config.read_text()
        assert any(d.path == tmp_path / "bird" / "bird.conf" for d in result.diffs)


class TestEmptyPeerIpIsSkipped:
    def test_missing_peer_ip_does_not_fail_whole_apply(self, tmp_path: Path) -> None:
        """空 peer_ip 只跳过这一个 bird 文件,不能让整个 apply 炸掉。"""
        cfg = _cfg(tmp_path)
        peer = _ibgp_peer()
        peer["peer_ip"] = ""
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()], ibgp=[peer])
        _seed_cache(cfg.cache_db_path, payload)

        result = _apply(cfg, tmp_path)
        names = {p.name for p in result.written}
        assert "ibgp_alpha.conf" not in names
        assert {"wg_alpha.netdev", "dn42_1234.conf", "babel.conf"} <= names


class TestExtraConfPlaceholder:
    """bird.conf include 了 extra.conf,该文件必须存在;而它的内容属于用户。"""

    def test_creates_placeholder_when_missing(self, tmp_path: Path) -> None:
        local_config = _local_config(tmp_path)
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        apply(node_config=cfg, config_path=local_config)

        extra = tmp_path / "bird" / "extra.conf"
        assert extra.exists()
        assert all(not line.strip() or line.lstrip().startswith("#") for line in extra.read_text().splitlines())
        assert str(extra) in (tmp_path / "bird" / "bird.conf").read_text()

    def test_reconcile_never_overwrites_user_content(self, tmp_path: Path) -> None:
        """agent 每 900 秒 reconcile 一次,占位内容进常规 diff 管线就会反复抹掉用户配置。"""
        local_config = _local_config(tmp_path)
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        extra = tmp_path / "bird" / "extra.conf"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("protocol static custom { ipv6; }\n")

        result = apply(node_config=cfg, config_path=local_config)

        assert extra.read_text() == "protocol static custom { ipv6; }\n"
        assert extra not in result.written
        assert all(d.path != extra for d in result.diffs)


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

        _apply(cfg, tmp_path, runner=self._recording_runner(seen))
        assert seen == [["networkctl", "reload"], ["birdc", "configure"]]

    def test_second_apply_with_no_changes_runs_nothing(self, tmp_path: Path) -> None:
        """agent 每 900 秒 reconcile 一次,无变更就 reload 会让每节点每天空跑 96 次。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)
        _apply(cfg, tmp_path, runner=self._recording_runner([]))

        seen: list[list[str]] = []
        result = _apply(cfg, tmp_path, runner=self._recording_runner(seen))
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
        _apply(cfg, tmp_path, no_reload=True, runner=self._recording_runner(seen))
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
        _apply(cfg, tmp_path, runner=self._recording_runner(seen))
        assert seen == []

    def test_failure_becomes_warning_not_exception(self, tmp_path: Path) -> None:
        from dn42ctl.services.reload import ReloadAction

        def failing(cmd: list[str]) -> ReloadAction:
            return ReloadAction(cmd=cmd, ok=False, error="boom")

        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        _seed_cache(cfg.cache_db_path, payload)

        result = _apply(cfg, tmp_path, runner=failing)
        assert len(result.warnings) == 2
        assert all("失败" in w for w in result.warnings)
        assert [a.ok for a in result.reloads] == [False, False]


class TestNodeBlockPathsAndBackend:
    def test_bird_conf_includes_match_where_peers_were_written(self, tmp_path: Path) -> None:
        """peer 文件、babel.conf 与 bird.conf 的 include 必须同出一源。
        分成两处会让 bird 去 include 一个空目录。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path, bgp=[_bgp_peer()])
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        _apply(cfg, tmp_path)
        locations = _paths(tmp_path)
        bird_conf = Path(locations["bird_conf_path"]).read_text()
        assert locations["peers_dir"] in bird_conf
        assert locations["babel_conf_path"] in bird_conf
        assert locations["bird_extra_conf_path"] in bird_conf
        assert (Path(locations["peers_dir"]) / "dn42_1234.conf").exists()

    def test_nm_dummy_backend_skips_networkd_dummy_files(self, tmp_path: Path) -> None:
        """dummy_backend=nm 时写 networkd 的 dn42-dummy.* 会造出与 NM 冲突的配置。"""
        cfg = _cfg(tmp_path)
        payload = _make_payload(tmp_path)
        payload["node"] = {"own_ipv6": "fd42:4242:1234::9"}
        _seed_cache(cfg.cache_db_path, payload)

        result = apply(node_config=cfg, config_path=_local_config(tmp_path, dummy_backend="nm"))
        assert not (tmp_path / "networkd" / "dn42-dummy.netdev").exists()
        assert not (tmp_path / "networkd" / "dn42-dummy.network").exists()
        assert any("NetworkManager" in w for w in result.warnings)
        # config.toml 与 bird.conf 仍然照常更新
        assert "fd42:4242:1234::9" in (tmp_path / "bird" / "bird.conf").read_text()
