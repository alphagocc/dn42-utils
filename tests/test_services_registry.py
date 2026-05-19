"""Tests for dn42ctl.services.registry — RPSL file parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.services.registry import (
    RegistryError,
    RegistryNotFoundError,
    read_aut_num,
    read_mntner_auth,
    read_pgp_key,
)


def test_read_aut_num_returns_mnt_by(dn42_registry: Path) -> None:
    names = read_aut_num(str(dn42_registry), 4242421234)
    assert names == ["TEST-MNT", "TEST2-MNT"]


def test_read_aut_num_missing_file(dn42_registry: Path) -> None:
    with pytest.raises(RegistryNotFoundError, match="不存在"):
        read_aut_num(str(dn42_registry), 9999999999)


def test_read_aut_num_invalid_asn(dn42_registry: Path) -> None:
    with pytest.raises(RegistryError, match="正整数"):
        read_aut_num(str(dn42_registry), -1)


def test_read_aut_num_registry_not_set() -> None:
    with pytest.raises(RegistryError, match="未配置"):
        read_aut_num(None, 1234)


def test_read_mntner_auth_ssh_and_pgp(dn42_registry: Path) -> None:
    options = read_mntner_auth(str(dn42_registry), "TEST-MNT")
    assert len(options) == 2
    assert options[0].scheme == "ssh-ed25519"
    assert options[0].index == 0
    assert "AAAAC3NzaC1lZDI1NTE5" in options[0].raw
    assert options[1].scheme == "pgp-fingerprint"
    assert options[1].index == 1
    assert options[1].fingerprint is not None
    assert len(options[1].fingerprint) == 40


def test_read_mntner_auth_ssh_only(dn42_registry: Path) -> None:
    options = read_mntner_auth(str(dn42_registry), "TEST2-MNT")
    assert len(options) == 1
    assert options[0].scheme == "ssh-rsa"


def test_read_mntner_auth_bad_name(dn42_registry: Path) -> None:
    with pytest.raises(RegistryError, match="非法"):
        read_mntner_auth(str(dn42_registry), "../etc/passwd")


def test_read_mntner_auth_not_found(dn42_registry: Path) -> None:
    with pytest.raises(RegistryNotFoundError):
        read_mntner_auth(str(dn42_registry), "NONEXISTENT-MNT")


def test_read_pgp_key(dn42_registry: Path) -> None:
    fp = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    key = read_pgp_key(str(dn42_registry), fp)
    assert "-----BEGIN PGP PUBLIC KEY BLOCK-----" in key
    assert "-----END PGP PUBLIC KEY BLOCK-----" in key


def test_read_pgp_key_preserves_blank_line(dn42_registry: Path) -> None:
    fp = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    key = read_pgp_key(str(dn42_registry), fp)
    lines = key.splitlines()
    begin_idx = lines.index("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert lines[begin_idx + 1] == "", "blank line after BEGIN header must be preserved"


def test_read_pgp_key_bad_fingerprint(dn42_registry: Path) -> None:
    with pytest.raises(RegistryError, match="非法"):
        read_pgp_key(str(dn42_registry), "ZZZZ")


def test_path_traversal_rejected(dn42_registry: Path) -> None:
    with pytest.raises(RegistryError):
        read_mntner_auth(str(dn42_registry), "..%2F..%2Fetc%2Fpasswd")
