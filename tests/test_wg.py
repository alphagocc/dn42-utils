from __future__ import annotations

import re
import subprocess
from unittest.mock import patch

import pytest

from dn42ctl.wg import WireGuardError, generate_random_lla_cidr, generate_wg_keypair, pubkey_from_private


class TestPubkeyFromPrivate:
    def test_success(self) -> None:
        with patch("subprocess.check_output", return_value="  PUBKEY_VALUE  \n") as mock:
            result = pubkey_from_private("PRIVKEY")
            assert result == "PUBKEY_VALUE"
            mock.assert_called_once()

    def test_wg_not_found(self) -> None:
        with (
            patch("subprocess.check_output", side_effect=FileNotFoundError()),
            pytest.raises(WireGuardError, match="wg"),
        ):
            pubkey_from_private("PRIVKEY")

    def test_wg_fails(self) -> None:
        with (
            patch(
                "subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "wg", output="error msg"),
            ),
            pytest.raises(WireGuardError, match="执行失败"),
        ):
            pubkey_from_private("PRIVKEY")


class TestGenerateWgKeypair:
    def test_success(self) -> None:
        def mock_check_output(cmd, **kwargs):
            if cmd == ["wg", "genkey"]:
                return "PRIVATE_KEY\n"
            if cmd == ["wg", "pubkey"]:
                return "PUBLIC_KEY\n"
            raise ValueError(f"unexpected cmd: {cmd}")

        with patch("subprocess.check_output", side_effect=mock_check_output):
            priv, pub = generate_wg_keypair()
            assert priv == "PRIVATE_KEY"
            assert pub == "PUBLIC_KEY"

    def test_wg_not_found(self) -> None:
        with (
            patch("subprocess.check_output", side_effect=FileNotFoundError()),
            pytest.raises(WireGuardError, match="wg"),
        ):
            generate_wg_keypair()

    def test_empty_key(self) -> None:
        with patch("subprocess.check_output", return_value="\n"), pytest.raises(WireGuardError, match="空密钥"):
            generate_wg_keypair()


class TestGenerateRandomLlaCidr:
    def test_format(self) -> None:
        for _ in range(20):
            result = generate_random_lla_cidr()
            assert result.startswith("fe80::")
            assert result.endswith("/64")
            assert re.match(r"fe80::[0-9a-f]{4}:[0-9a-f]{4}/64", result)

    def test_varies(self) -> None:
        results = {generate_random_lla_cidr() for _ in range(50)}
        assert len(results) > 1
