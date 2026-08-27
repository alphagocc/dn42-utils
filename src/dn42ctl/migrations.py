from __future__ import annotations

import sqlite3
from collections.abc import Callable

# 迁移步骤要么是一段 SQL(走 executescript,必须幂等),要么是一个可调用对象。
#
# ALTER TABLE ADD COLUMN **只能**走可调用分支:SQLite 没有 ADD COLUMN IF NOT EXISTS,
# 而 executescript 会在执行前隐式 COMMIT —— 脚本中途失败会留下"前几列已提交、版本号
# 没写、rollback() 对它们无效"的状态,重跑直接 duplicate column,库永久卡死。
# 可调用分支跑在连接的隐式事务里,与 schema_migrations 插入真正原子。
MigrationStep = str | Callable[[sqlite3.Connection], None]


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """幂等的 ADD COLUMN。表名/列名全部是本模块的字面量,不来自任何外部输入。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")  # noqa: S608


def _migration_10(conn: sqlite3.Connection) -> None:
    """节点地址集中管理 + iBGP mesh 反向链接。

    四列全部可空且**不给 DEFAULT**。NULL 的语义是"该字段未纳入中心管理":desired state
    不下发它,节点 config.toml 里的现有值原样保留。这是升级瞬间不砸掉一台正在正常工作的
    节点的唯一安全默认值。

    endpoint_host 只存主机、不含端口 —— 端口是对端每条隧道的 listen_port,存不进节点级字段。

    remote_node_id 刻意不加 FOREIGN KEY:删除节点 A 不能级联删掉节点 B 指向 A 的 peer 行
    (那是 B 的配置,不是 A 的)。悬空引用无害,传播时查不到地址就跳过并告警。

    语义与传播规则详见 docs/architecture/node_addressing.md。
    """
    ensure_column(conn, "managed_nodes", "endpoint_host", "TEXT")
    ensure_column(conn, "managed_nodes", "own_ipv6", "TEXT")
    ensure_column(conn, "managed_nodes", "router_id", "TEXT")
    ensure_column(conn, "ibgp_peers", "remote_node_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ibgp_peers_remote ON ibgp_peers(remote_node_id)")


MIGRATIONS: list[tuple[int, MigrationStep]] = [
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
            babel_rxcost INTEGER NOT NULL DEFAULT 20,
            peer_ip TEXT,
            has_wg INTEGER NOT NULL DEFAULT 1,
            babel_type TEXT NOT NULL DEFAULT 'tunnel',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(node_id, name),
            UNIQUE(node_id, ifname),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

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

        CREATE TABLE IF NOT EXISTS node_desired_pin (
            node_id TEXT PRIMARY KEY,
            revision TEXT NOT NULL,
            pinned_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        """.strip(),
    ),
    (
        8,
        """
        UPDATE bgp_peers SET net_backend = 'networkd' WHERE net_backend = 'nm';
        UPDATE ibgp_peers SET net_backend = 'networkd' WHERE net_backend = 'nm';
        """.strip(),
    ),
    (
        9,
        # 变更通知队列。`dn42ctl serve` 的 watcher 轮询这张表,把变更转成节点 WS 推送。
        #
        # AUTOINCREMENT 是必需的(不是风格问题):裸 INTEGER PRIMARY KEY 在最大行被删除后会
        # 复用 rowid,而裁剪会常规性地删行 —— 一旦复用,watcher 游标会静默倒退并丢事件。
        #
        # 不加 FOREIGN KEY:remove_node 必须发 access_revoked,那行得在节点被删除后存活。
        """
        CREATE TABLE IF NOT EXISTS sync_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sync_events_node ON sync_events(node_id, id);
        """.strip(),
    ),
    (10, _migration_10),
    (
        11,
        # 作废所有非 sha256$ 前缀的 token hash。旧格式无法转换(哈希不可逆),只能让
        # 管理员重签。self 节点由 serve_bootstrap 在下次启动时自动补上,远程节点需
        # 人工 rotate —— 这是一次有感知的中断,升级步骤见 docs/architecture/deployment.md。
        """
        UPDATE managed_nodes SET api_token_hash = NULL
         WHERE api_token_hash IS NOT NULL AND api_token_hash NOT LIKE 'sha256$%';
        """.strip(),
    ),
    (
        12,
        # 存量的多 self 行:保留 updated_at 最新的一行,其余降级为普通受管节点。
        # upsert_self 每次 serve 启动都会刷新当前 self 的 updated_at,所以"最新"就是
        # 当前 self_node_id 指向的那行。降级而非删除——旧分区里的 peer 一条没少。
        """
        UPDATE managed_nodes SET is_self = 0
         WHERE is_self = 1
           AND node_id NOT IN (
             SELECT node_id FROM managed_nodes WHERE is_self = 1
              ORDER BY updated_at DESC, node_id DESC LIMIT 1
           );
        """.strip(),
    ),
]
