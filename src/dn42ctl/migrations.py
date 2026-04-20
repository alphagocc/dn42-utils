from __future__ import annotations


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bgp_peers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            peer_asn INTEGER NOT NULL,
            ifname TEXT NOT NULL,
            wg_private_key TEXT NOT NULL,
            wg_public_key TEXT NOT NULL,
            peer_public_key TEXT,
            endpoint TEXT,
            local_lla TEXT NOT NULL,
            peer_lla TEXT,
            listen_port INTEGER NOT NULL,
            allowed_ips_json TEXT NOT NULL,
            net_backend TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(node_id, peer_asn),
            UNIQUE(node_id, ifname),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ibgp_peers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            name TEXT NOT NULL,
            ifname TEXT NOT NULL,
            wg_private_key TEXT NOT NULL,
            wg_public_key TEXT NOT NULL,
            peer_public_key TEXT,
            endpoint TEXT,
            local_lla TEXT NOT NULL,
            peer_lla TEXT,
            listen_port INTEGER NOT NULL,
            allowed_ips_json TEXT NOT NULL,
            net_backend TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(node_id, name),
            UNIQUE(node_id, ifname),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        """.strip(),
    ),
    (
        2,
        """
        ALTER TABLE ibgp_peers
        ADD COLUMN babel_rxcost INTEGER NOT NULL DEFAULT 120;
        """.strip(),
    ),
]
