"""Tests for dn42ctl.services.crypto_verify — SSH / PGP subprocess wrappers.

These tests are skipped if ssh-keygen / gpg are not available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dn42ctl.services.crypto_verify import verify_pgp, verify_ssh

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen not found on PATH",
)


@pytest.fixture
def ssh_keypair(tmp_path: Path) -> tuple[Path, str]:
    """Generate an ephemeral ed25519 SSH keypair, return (private_key_path, pubkey_line)."""
    priv = tmp_path / "id_test"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-q"],
        check=True,
    )
    pubkey = (tmp_path / "id_test.pub").read_text("utf-8").strip()
    return priv, pubkey


def test_verify_ssh_valid(tmp_path: Path, ssh_keypair: tuple[Path, str]) -> None:
    priv, pubkey = ssh_keypair
    msg = b"test-challenge-nonce-1234"
    msg_file = tmp_path / "msg.txt"
    msg_file.write_bytes(msg)
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", "test-ns", "-f", str(priv), str(msg_file)],
        check=True,
    )
    sig = (tmp_path / "msg.txt.sig").read_text("utf-8")
    assert verify_ssh(
        message=msg,
        signature=sig,
        allowed_pubkey=pubkey,
        namespace="test-ns",
        identity="test@dn42",
    )


def test_verify_ssh_wrong_key(tmp_path: Path, ssh_keypair: tuple[Path, str]) -> None:
    priv, _ = ssh_keypair
    msg = b"challenge-abc"
    msg_file = tmp_path / "msg.txt"
    msg_file.write_bytes(msg)
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", "test-ns", "-f", str(priv), str(msg_file)],
        check=True,
    )
    sig = (tmp_path / "msg.txt.sig").read_text("utf-8")
    # generate a different key to use for verification
    priv2 = tmp_path / "id_other"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv2), "-N", "", "-q"],
        check=True,
    )
    other_pubkey = (tmp_path / "id_other.pub").read_text("utf-8").strip()
    assert not verify_ssh(
        message=msg,
        signature=sig,
        allowed_pubkey=other_pubkey,
        namespace="test-ns",
        identity="test@dn42",
    )


def test_verify_ssh_wrong_message(tmp_path: Path, ssh_keypair: tuple[Path, str]) -> None:
    priv, pubkey = ssh_keypair
    msg_file = tmp_path / "msg.txt"
    msg_file.write_bytes(b"original")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", "ns", "-f", str(priv), str(msg_file)],
        check=True,
    )
    sig = (tmp_path / "msg.txt.sig").read_text("utf-8")
    assert not verify_ssh(
        message=b"tampered",
        signature=sig,
        allowed_pubkey=pubkey,
        namespace="ns",
        identity="x",
    )


def test_verify_ssh_empty_sig() -> None:
    assert not verify_ssh(
        message=b"x",
        signature="",
        allowed_pubkey="ssh-ed25519 AAAA test",
        namespace="ns",
        identity="x",
    )


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not found on PATH")
class TestPGPVerify:
    def test_verify_pgp_valid(self, tmp_path: Path) -> None:
        home = tmp_path / "gnupg"
        home.mkdir(mode=0o700)
        common = ["gpg", "--homedir", str(home), "--batch", "--no-tty", "--quiet"]
        # generate a key
        subprocess.run(
            common
            + [
                "--quick-gen-key",
                "--passphrase",
                "",
                "test@dn42",
                "default",
                "default",
                "0",
            ],
            check=True,
        )
        # export pubkey
        exp = subprocess.run(
            common + ["--armor", "--export", "test@dn42"],
            capture_output=True,
            check=True,
        )
        ascii_key = exp.stdout.decode("utf-8")
        msg = b"challenge-nonce-xyz"
        msg_file = tmp_path / "msg.txt"
        msg_file.write_bytes(msg)
        # clear-sign
        subprocess.run(
            common + ["--passphrase", "", "--pinentry-mode", "loopback", "--clearsign", str(msg_file)],
            check=True,
        )
        signed = (tmp_path / "msg.txt.asc").read_text("utf-8")
        assert verify_pgp(message=msg, signature=signed, ascii_key=ascii_key)

    def test_verify_pgp_tampered_message(self, tmp_path: Path) -> None:
        home = tmp_path / "gnupg"
        home.mkdir(mode=0o700)
        common = ["gpg", "--homedir", str(home), "--batch", "--no-tty", "--quiet"]
        subprocess.run(
            common
            + [
                "--quick-gen-key",
                "--passphrase",
                "",
                "t2@dn42",
                "default",
                "default",
                "0",
            ],
            check=True,
        )
        exp = subprocess.run(
            common + ["--armor", "--export", "t2@dn42"],
            capture_output=True,
            check=True,
        )
        ascii_key = exp.stdout.decode("utf-8")
        msg_file = tmp_path / "msg.txt"
        msg_file.write_bytes(b"real")
        subprocess.run(
            common + ["--passphrase", "", "--pinentry-mode", "loopback", "--clearsign", str(msg_file)],
            check=True,
        )
        signed = (tmp_path / "msg.txt.asc").read_text("utf-8")
        assert not verify_pgp(message=b"tampered", signature=signed, ascii_key=ascii_key)
