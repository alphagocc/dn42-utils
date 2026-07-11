from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.config import AppConfig, ConfigError, load_config, save_config


class TestSaveAndLoadRoundtrip:
    def test_roundtrip(self, tmp_path: Path, sample_config: AppConfig) -> None:
        cfg_path = tmp_path / "config.toml"
        save_config(cfg_path, sample_config)
        loaded = load_config(cfg_path)
        assert loaded.node_id == sample_config.node_id
        assert loaded.own_asn == sample_config.own_asn
        assert loaded.router_id == sample_config.router_id
        assert loaded.own_ipv6 == sample_config.own_ipv6
        assert loaded.ownnet_v6 == sample_config.ownnet_v6
        assert loaded.ownnetset_v6 == sample_config.ownnetset_v6
        assert loaded.bird_conf_path == sample_config.bird_conf_path
        assert loaded.bird_peers_dir == sample_config.bird_peers_dir
        assert loaded.networkd_dir == sample_config.networkd_dir
        assert loaded.dummy_backend == sample_config.dummy_backend

    def test_creates_parent_dirs(self, tmp_path: Path, sample_config: AppConfig) -> None:
        cfg_path = tmp_path / "sub" / "dir" / "config.toml"
        save_config(cfg_path, sample_config)
        assert cfg_path.exists()


class TestLoadConfig:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.toml")

    def test_malformed_toml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "bad.toml"
        cfg_path.write_text("this is not [valid toml !!!")
        with pytest.raises(ConfigError):
            load_config(cfg_path)

    def test_missing_paths_section(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "nopaths.toml"
        cfg_path.write_text(
            'node_id = "test"\nown_asn = 1234\nrouter_id = "1.2.3.4"\n'
            'own_ipv6 = "fd42::1"\nownnet_v6 = "fd42::/48"\n'
            'ownnetset_v6 = "[fd42::/48+]"\n'
        )
        with pytest.raises(ConfigError, match="paths"):
            load_config(cfg_path)

    def test_invalid_asn(self, tmp_path: Path, sample_config: AppConfig) -> None:
        cfg_path = tmp_path / "config.toml"
        save_config(cfg_path, sample_config)
        content = cfg_path.read_text()
        content = content.replace(f"own_asn = {sample_config.own_asn}", "own_asn = -1")
        cfg_path.write_text(content)
        with pytest.raises(ConfigError, match="不合法"):
            load_config(cfg_path)


class TestSaveConfig:
    def test_chmod_called(self, tmp_path: Path, sample_config: AppConfig) -> None:
        cfg_path = tmp_path / "config.toml"
        with patch("dn42ctl.config.chmod_best_effort") as mock_chmod:
            save_config(cfg_path, sample_config)
            mock_chmod.assert_called_once_with(cfg_path, 0o600)
