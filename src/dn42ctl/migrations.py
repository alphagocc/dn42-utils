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
    (
        3,
        """
        ALTER TABLE ibgp_peers ADD COLUMN peer_ip TEXT;
        ALTER TABLE ibgp_peers ADD COLUMN has_wg INTEGER NOT NULL DEFAULT 1;
        """.strip(),
    ),
    (
        4,
        """
        ALTER TABLE ibgp_peers ADD COLUMN babel_type TEXT NOT NULL DEFAULT 'tunnel';
        """.strip(),
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS managed_nodes (
            node_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            api_token_hash TEXT,
            write_policy TEXT NOT NULL DEFAULT
                '{"peer_add":"review","peer_modify":"review","peer_delete":"review","report":"auto"}',
            enabled INTEGER NOT NULL DEFAULT 1,
            is_self INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS config_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            received_at TEXT NOT NULL,
            decided_at TEXT,
            message TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_config_proposals_node_status
            ON config_proposals(node_id, status);

        CREATE TABLE IF NOT EXISTS node_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            received_at TEXT NOT NULL,
            imported_at TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_node_reports_node_kind
            ON node_reports(node_id, kind, received_at);

        CREATE TABLE IF NOT EXISTS config_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            revision TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(node_id, revision),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_config_revisions_node_time
            ON config_revisions(node_id, generated_at);
        """.strip(),
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS node_desired_pin (
            node_id TEXT PRIMARY KEY,
            revision TEXT NOT NULL,
            pinned_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        """.strip(),
    ),
    (
        7,
        """
        UPDATE bgp_peers SET local_lla = SUBSTR(local_lla, 1, INSTR(local_lla, '/') - 1)
            WHERE local_lla LIKE '%/%';
        UPDATE ibgp_peers SET local_lla = SUBSTR(local_lla, 1, INSTR(local_lla, '/') - 1)
            WHERE local_lla LIKE '%/%';
        """.strip(),
    ),
]
