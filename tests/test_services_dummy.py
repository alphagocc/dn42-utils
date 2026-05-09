from __future__ import annotations

import subprocess
from unittest.mock import patch

from dn42ctl.services.dummy import (
    _address_bound,
    _interface_exists,
    detect_dummy_backend,
    ensure_dummy_interface,
)


class TestDetectDummyBackend:
    def test_nm_available(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/nmcli"),
            patch("subprocess.check_output", return_value="running\n"),
        ):
            assert detect_dummy_backend() == "nm"

    def test_nmcli_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            assert detect_dummy_backend() == "iproute2"

    def test_nmcli_fails(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/nmcli"),
            patch(
                "subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "nmcli"),
            ),
        ):
            assert detect_dummy_backend() == "iproute2"


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


class TestEnsureDummyInterface:
    def test_already_exists_and_bound(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=True),
            patch("dn42ctl.services.dummy._address_bound", return_value=True),
        ):
            result = ensure_dummy_interface("fd42:4242:1234::1")
            assert result.skipped is True
            assert result.created is False

    def test_exists_but_not_bound_iproute2(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=True),
            patch("dn42ctl.services.dummy._address_bound", return_value=False),
            patch(
                "dn42ctl.services.dummy.detect_dummy_backend",
                return_value="iproute2",
            ),
            patch("subprocess.check_output", return_value=""),
        ):
            result = ensure_dummy_interface("fd42:4242:1234::1")
            assert result.created is True
            assert result.backend == "iproute2"

    def test_not_exists_create_iproute2(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=False),
            patch(
                "dn42ctl.services.dummy.detect_dummy_backend",
                return_value="iproute2",
            ),
            patch("subprocess.check_output", return_value=""),
        ):
            result = ensure_dummy_interface("fd42:4242:1234::1")
            assert result.created is True
            assert result.backend == "iproute2"

    def test_creation_fails(self) -> None:
        with (
            patch("dn42ctl.services.dummy._interface_exists", return_value=False),
            patch(
                "dn42ctl.services.dummy.detect_dummy_backend",
                return_value="iproute2",
            ),
            patch(
                "subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "ip"),
            ),
        ):
            result = ensure_dummy_interface("fd42:4242:1234::1")
            assert result.created is False
            assert len(result.warnings) > 0
