from __future__ import annotations

from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/dn42ctl/config.toml")
DEFAULT_DB_PATH = Path("/var/lib/dn42ctl/dn42.sqlite3")

DEFAULT_BIRD_DIR = Path("/etc/bird")
DEFAULT_BIRD_CONF_PATH = DEFAULT_BIRD_DIR / "bird.conf"
DEFAULT_BIRD_PEERS_DIR = DEFAULT_BIRD_DIR / "peers"
DEFAULT_BIRD_BABEL_CONF_PATH = DEFAULT_BIRD_DIR / "babel.conf"
DEFAULT_BIRD_ROA_V6_CONF_PATH = DEFAULT_BIRD_DIR / "roa_dn42_v6.conf"

DEFAULT_NETWORKD_DIR = Path("/etc/systemd/network")
DEFAULT_NM_SYSTEM_CONNECTIONS_DIR = Path("/etc/NetworkManager/system-connections")
