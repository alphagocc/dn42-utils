from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from dn42ctl.config import AppConfig
from dn42ctl.db import Database

VALID_PUBKEY = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="
VALID_ENDPOINT = "example.com:51820"
VALID_PEER_LLA = "fe80::1"
VALID_PEER_IP = "fd42:4242:5678::1"
FAKE_WG_PRIVKEY = "cFYxMU1qZEdOcUI3RHBOS0FRUUVMVmR3aFNTa1F3VT0="
FAKE_WG_PUBKEY = "dGVzdHB1YmxpY2tleWZvcnVuaXR0ZXN0aW5nMTIzNA=="


@pytest.fixture
def sample_config(tmp_path: Path) -> AppConfig:
    bird_dir = tmp_path / "bird"
    bird_dir.mkdir()
    peers_dir = bird_dir / "peers"
    peers_dir.mkdir()
    networkd_dir = tmp_path / "networkd"
    networkd_dir.mkdir()
    nm_dir = tmp_path / "nm"
    nm_dir.mkdir()
    return AppConfig(
        node_id="test-node",
        own_asn=4242421234,
        router_id="172.23.0.1",
        own_ipv6="fd42:4242:1234::1",
        ownnet_v6="fd42:4242:1234::/48",
        ownnetset_v6="[fd42:4242:1234::/48+]",
        bird_conf_path=str(bird_dir / "bird.conf"),
        bird_peers_dir=str(peers_dir),
        bird_babel_conf_path=str(bird_dir / "babel.conf"),
        bird_roa_v6_conf_path=str(bird_dir / "roa_dn42_v6.conf"),
        networkd_dir=str(networkd_dir),
        nm_system_connections_dir=str(nm_dir),
    )


@pytest.fixture
def mem_db() -> Database:
    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    db.migrate()
    return db


@pytest.fixture
def mem_db_with_node(mem_db: Database) -> Database:
    mem_db.ensure_node("test-node")
    return mem_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite3"


@pytest.fixture
def mock_wg_keypair():
    with patch(
        "dn42ctl.services.core.generate_wg_keypair",
        return_value=(FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY),
    ) as m:
        yield m


@pytest.fixture
def dn42_registry(tmp_path: Path) -> Path:
    """Create a minimal fake dn42 registry tree for auto-peer tests."""
    root = tmp_path / "registry"
    (root / "data" / "aut-num").mkdir(parents=True)
    (root / "data" / "mntner").mkdir(parents=True)
    (root / "data" / "key-cert").mkdir(parents=True)

    # aut-num with two mnt-by lines
    (root / "data" / "aut-num" / "AS4242421234").write_text(
        "aut-num:            AS4242421234\n"
        "as-name:            TEST-AS\n"
        "mnt-by:             TEST-MNT\n"
        "mnt-by:             TEST2-MNT\n"
        "source:             DN42\n",
        encoding="utf-8",
    )

    # mntner with ssh + pgp auth
    (root / "data" / "mntner" / "TEST-MNT").write_text(
        "mntner:             TEST-MNT\n"
        "admin-c:            TEST-DN42\n"
        "auth:               ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake1234567890FakeKey test@dn42\n"
        "auth:               pgp-fingerprint AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "auth:               ed25519-pw somepwhash\n"
        "mnt-by:             TEST-MNT\n"
        "source:             DN42\n",
        encoding="utf-8",
    )

    # second mntner with only ssh
    (root / "data" / "mntner" / "TEST2-MNT").write_text(
        "mntner:             TEST2-MNT\n"
        "auth:               ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC test2@dn42\n"
        "mnt-by:             TEST2-MNT\n"
        "source:             DN42\n",
        encoding="utf-8",
    )

    # pgp key-cert
    (root / "data" / "key-cert" / "PGPKEY-AAAAAAAA").write_text(
        "key-cert:           PGPKEY-AAAAAAAA\n"
        "method:             PGP\n"
        "fingerpr:           AAAA AAAA AAAA AAAA AAAA AAAA AAAA AAAA AAAA AAAA\n"
        "certif:             -----BEGIN PGP PUBLIC KEY BLOCK-----\n"
        "certif:             \n"
        "certif:             mQENBFake\n"
        "certif:             =fake\n"
        "certif:             -----END PGP PUBLIC KEY BLOCK-----\n"
        "mnt-by:             TEST-MNT\n"
        "source:             DN42\n",
        encoding="utf-8",
    )

    return root
