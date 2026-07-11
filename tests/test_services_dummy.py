from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from dn42ctl.services.dummy import (
    _address_bound,
    _interface_exists,
    ensure_dummy_interface,
)


class TestInterfaceExists:
    def test_exists(self) -> None:
        with patch("subprocess.check_output", return_value=""):
            assert _interface_exists("dn42-dummy") is True

    def test_not_exists(self) -> None:
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "ip"),
        ):
            assert _interface_exists("dn42-dummy") is False


class TestAddressBound:
    def test_bound(self) -> None:
        with patch(
            "subprocess.check_output",
            return_value="inet6 fd42:4242:1234::1/128 scope global\n",
        ):
            assert _address_bound("dn42-dummy", "fd42:4242:1234::1/128") is True

    def test_not_bound(self) -> None:
        with patch("subprocess.check_output", return_value=""):
            assert _address_bound("dn42-dummy", "fd42:4242:1234::1/128") is False


class TestEnsureDummyInterfaceNetworkd:
    def test_already_up_to_date(self, tmp_path: Path) -> None:
        netdev = tmp_path / "dn42-dummy.netdev"
        network = tmp_path / "dn42-dummy.network"
        netdev.write_text("[NetDev]\nName=dn42-dummy\nKind=dummy\n")
        network.write_text(
            "[Match]\nName=dn42-dummy\n\n[Network]\nDHCP=no\nIPv6AcceptRA=false\n\n"
            "[Address]\nAddress=fd42:4242:1234::1/128\n"
        )
        result = ensure_dummy_interface(
            "fd42:4242:1234::1", backend="networkd", networkd_dir=str(tmp_path)
        )
        assert result.skipped is True
        assert result.created is False
        assert result.backend == "networkd"

    def test_creates_files_and_reloads(self, tmp_path: Path) -> None:
        with patch("subprocess.check_output", return_value="") as mock_run:
            result = ensure_dummy_interface(
                "fd42:4242:1234::1", backend="networkd", networkd_dir=str(tmp_path)
            )
        assert result.created is True
        assert result.backend == "networkd"
        assert (tmp_path / "dn42-dummy.netdev").exists()
        assert (tmp_path / "dn42-dummy.network").exists()
        mock_run.assert_called_once_with(
            ["networkctl", "reload"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10,
        )

    def test_write_failure(self, tmp_path: Path) -> None:
        read_only = tmp_path / "no_write"
        read_only.mkdir()
        read_only.chmod(0o555)
        result = ensure_dummy_interface(
            "fd42:4242:1234::1", backend="networkd", networkd_dir=str(read_only)
        )
        assert result.created is False
        assert len(result.warnings) > 0
        read_only.chmod(0o755)


class TestEnsureDummyInterfaceNM:
    def test_already_exists_and_bound(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=True),
            patch("dn42ctl.services.dummy._address_bound", return_value=True),
        ):
            result = ensure_dummy_interface(
                "fd42:4242:1234::1", backend="nm", networkd_dir="/etc/systemd/network"
            )
            assert result.skipped is True
            assert result.created is False
            assert result.backend == "nm"

    def test_exists_but_not_bound(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=True),
            patch("dn42ctl.services.dummy._address_bound", return_value=False),
            patch("subprocess.check_output", return_value=""),
        ):
            result = ensure_dummy_interface(
                "fd42:4242:1234::1", backend="nm", networkd_dir="/etc/systemd/network"
            )
            assert result.created is True
            assert result.backend == "nm"

    def test_not_exists_creates(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=False),
            patch("subprocess.check_output", return_value=""),
        ):
            result = ensure_dummy_interface(
                "fd42:4242:1234::1", backend="nm", networkd_dir="/etc/systemd/network"
            )
            assert result.created is True
            assert result.backend == "nm"

    def test_creation_fails(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=False),
            patch(
                "subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "nmcli"),
            ),
        ):
            result = ensure_dummy_interface(
                "fd42:4242:1234::1", backend="nm", networkd_dir="/etc/systemd/network"
            )
            assert result.created is False
            assert len(result.warnings) > 0
