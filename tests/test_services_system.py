from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dn42ctl.config import AppConfig
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.system import (
    _NFT_RULES,
    install_firewalld_conf,
    install_nftables_conf,
    install_roa_service,
    uninstall_firewalld_conf,
    uninstall_nftables_conf,
    uninstall_roa_service,
)


class TestFirewalldConf:
    def test_install_sets_rpfilter_no(self, tmp_path: Path) -> None:
        conf = tmp_path / "firewalld.conf"
        conf.write_text("IPv6_rpfilter=yes\nother=value\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._FIREWALLD_CONF", conf),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = install_firewalld_conf()

        assert conf.read_text() == "IPv6_rpfilter=no\nother=value\n"
        assert str(conf) in result.changed_files
        assert result.component == "firewalld-conf"
        assert result.action == "install"
        mock_run.assert_called_once_with(["systemctl", "restart", "firewalld"])

    def test_install_already_no_skips(self, tmp_path: Path) -> None:
        conf = tmp_path / "firewalld.conf"
        conf.write_text("IPv6_rpfilter=no\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._FIREWALLD_CONF", conf),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = install_firewalld_conf()

        assert result.changed_files == []
        assert any("跳过" in w for w in result.warnings)

    def test_uninstall_sets_rpfilter_yes(self, tmp_path: Path) -> None:
        conf = tmp_path / "firewalld.conf"
        conf.write_text("IPv6_rpfilter=no\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._FIREWALLD_CONF", conf),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = uninstall_firewalld_conf()

        assert conf.read_text() == "IPv6_rpfilter=yes\n"
        assert result.action == "uninstall"

    def test_missing_conf_raises(self, tmp_path: Path) -> None:
        conf = tmp_path / "nonexistent.conf"
        with (
            patch("dn42ctl.services.system._FIREWALLD_CONF", conf),
            pytest.raises(Dn42CtlError, match="不存在"),
        ):
            install_firewalld_conf()

    def test_missing_rpfilter_setting_raises(self, tmp_path: Path) -> None:
        conf = tmp_path / "firewalld.conf"
        conf.write_text("SomeOtherSetting=yes\n")

        with (
            patch("dn42ctl.services.system._FIREWALLD_CONF", conf),
            pytest.raises(Dn42CtlError, match="未在.*找到"),
        ):
            install_firewalld_conf()


class TestNftablesConf:
    def test_install_writes_rule_and_include(self, tmp_path: Path) -> None:
        nft_conf = tmp_path / "nftables.conf"
        nft_conf.write_text("# existing config\n")
        nft_rule = tmp_path / "dn42-no-conntrack.nft"

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._NFT_RULE_PATH", nft_rule),
            patch("dn42ctl.services.system._NFT_CONF_CANDIDATES", [nft_conf]),
            patch("dn42ctl.services.system._NFT_INCLUDE_LINE", f'include "{nft_rule}"'),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = install_nftables_conf()

        assert nft_rule.exists()
        assert nft_rule.read_text() == _NFT_RULES
        assert f'include "{nft_rule}"' in nft_conf.read_text()
        assert result.component == "nftables-conf"
        assert result.action == "install"
        assert mock_run.call_count == 2

    def test_install_skips_existing_include(self, tmp_path: Path) -> None:
        nft_rule = tmp_path / "dn42-no-conntrack.nft"
        include_line = f'include "{nft_rule}"'
        nft_conf = tmp_path / "nftables.conf"
        nft_conf.write_text(f"# existing\n{include_line}\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._NFT_RULE_PATH", nft_rule),
            patch("dn42ctl.services.system._NFT_CONF_CANDIDATES", [nft_conf]),
            patch("dn42ctl.services.system._NFT_INCLUDE_LINE", include_line),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = install_nftables_conf()

        assert any("已存在" in w for w in result.warnings)
        assert str(nft_conf) not in result.changed_files

    def test_install_no_conf_found_raises(self, tmp_path: Path) -> None:
        nft_rule = tmp_path / "dn42-no-conntrack.nft"

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._NFT_RULE_PATH", nft_rule),
            patch("dn42ctl.services.system._NFT_CONF_CANDIDATES", [tmp_path / "missing.conf"]),
            patch("dn42ctl.services.system._run", mock_run),
            pytest.raises(Dn42CtlError, match="未找到 nftables.conf"),
        ):
            install_nftables_conf()

    def test_uninstall_removes_rule_and_include(self, tmp_path: Path) -> None:
        nft_rule = tmp_path / "dn42-no-conntrack.nft"
        nft_rule.write_text(_NFT_RULES)
        include_line = f'include "{nft_rule}"'
        nft_conf = tmp_path / "nftables.conf"
        nft_conf.write_text(f"# existing\n{include_line}\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._NFT_RULE_PATH", nft_rule),
            patch("dn42ctl.services.system._NFT_CONF_CANDIDATES", [nft_conf]),
            patch("dn42ctl.services.system._NFT_INCLUDE_LINE", include_line),
            patch("dn42ctl.services.system._NFT_TABLE_NAME", "inet dn42_notrack"),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = uninstall_nftables_conf()

        assert not nft_rule.exists()
        assert include_line not in nft_conf.read_text()
        assert result.action == "uninstall"

    def test_uninstall_missing_rule_warns(self, tmp_path: Path) -> None:
        nft_rule = tmp_path / "missing.nft"
        nft_conf = tmp_path / "nftables.conf"
        nft_conf.write_text("# existing\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._NFT_RULE_PATH", nft_rule),
            patch("dn42ctl.services.system._NFT_CONF_CANDIDATES", [nft_conf]),
            patch("dn42ctl.services.system._NFT_INCLUDE_LINE", f'include "{nft_rule}"'),
            patch("dn42ctl.services.system._NFT_TABLE_NAME", "inet dn42_notrack"),
            patch("dn42ctl.services.system._run", mock_run),
        ):
            result = uninstall_nftables_conf()

        assert any("不存在" in w for w in result.warnings)


class TestRoaService:
    def test_install_writes_units(self, sample_config: AppConfig) -> None:
        mock_run = MagicMock()
        unit_dir = Path(sample_config.bird_roa_v6_conf_path).parent

        with (
            patch("dn42ctl.services.system._SYSTEMD_UNIT_DIR", unit_dir),
            patch("dn42ctl.services.system._run", mock_run),
            patch("shutil.which", return_value="/usr/bin/systemctl"),
        ):
            result = install_roa_service(config=sample_config)

        assert result.component == "roa-service"
        assert result.action == "install"
        assert len(result.changed_files) == 2
        assert mock_run.call_count == 3

    def test_install_no_systemctl_raises(self, sample_config: AppConfig) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(Dn42CtlError, match="systemctl"),
        ):
            install_roa_service(config=sample_config)

    def test_install_no_curl_warns(self, sample_config: AppConfig) -> None:
        mock_run = MagicMock()
        unit_dir = Path(sample_config.bird_roa_v6_conf_path).parent

        def mock_which(cmd: str) -> str | None:
            if cmd == "systemctl":
                return "/usr/bin/systemctl"
            return None

        with (
            patch("dn42ctl.services.system._SYSTEMD_UNIT_DIR", unit_dir),
            patch("dn42ctl.services.system._run", mock_run),
            patch("shutil.which", side_effect=mock_which),
        ):
            result = install_roa_service(config=sample_config)

        assert any("curl" in w for w in result.warnings)

    def test_uninstall_removes_units(self, tmp_path: Path) -> None:
        service = tmp_path / "dn42-roa-v6.service"
        timer = tmp_path / "dn42-roa-v6.timer"
        service.write_text("[Unit]\n")
        timer.write_text("[Unit]\n")

        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._SYSTEMD_UNIT_DIR", tmp_path),
            patch("dn42ctl.services.system._run", mock_run),
            patch("shutil.which", return_value="/usr/bin/systemctl"),
        ):
            result = uninstall_roa_service()

        assert not service.exists()
        assert not timer.exists()
        assert result.action == "uninstall"
        assert len(result.changed_files) == 2

    def test_uninstall_missing_units_warns(self, tmp_path: Path) -> None:
        mock_run = MagicMock()
        with (
            patch("dn42ctl.services.system._SYSTEMD_UNIT_DIR", tmp_path),
            patch("dn42ctl.services.system._run", mock_run),
            patch("shutil.which", return_value="/usr/bin/systemctl"),
        ):
            result = uninstall_roa_service()

        assert any("不存在" in w for w in result.warnings)

    def test_uninstall_no_systemctl_raises(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(Dn42CtlError, match="systemctl"),
        ):
            uninstall_roa_service()
