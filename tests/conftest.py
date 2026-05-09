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
