"""Read-only database browsing.

Read-only by design. Every authoritative write must go through the service layer,
because the change notification has to be emitted in the same transaction as the
business write (`db.emit_sync_event`). A generic UPDATE endpoint would bypass that
invariant, leaving the row changed, `sync_events` unwritten, and the node holding
stale config forever with no warning anywhere.

See `docs/architecture/db_browse.md` for the request/response formats, the table
allow-list and the redaction rules.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dn42ctl.db import Database
from dn42ctl.services.core import Dn42CtlError

REDACTED = "***"

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# Allow-list of table names. NEVER derive this from sqlite_master: a URL must never
# be able to name a table. The value is the set of columns that must be redacted for
# that table. Adding a table means classifying it here, which makes "does this new
# table hold secrets" a question someone is forced to answer.
#
# The rule: redact a column if and only if it stores a secret AND no existing admin
# route already returns that secret in the clear.
#   - config_revisions.payload_json is a full desired-state snapshot containing every
#     wg_private_key, and _revision_to_dict deliberately never returns it. Redacting
#     is mandatory here; missing it would publish the whole mesh's private keys.
#   - config_proposals / node_reports payload_json is already returned in full by
#     GET /api/admin/nodes/{id}/{proposals,reports}, so masking it here would only be
#     fooling ourselves.
BROWSABLE_TABLES: dict[str, tuple[str, ...]] = {
    "schema_migrations": (),
    "nodes": (),
    "bgp_peers": ("wg_private_key",),
    "ibgp_peers": ("wg_private_key",),
    "managed_nodes": ("api_token_hash",),
    "config_proposals": (),
    "node_reports": (),
    "config_revisions": ("payload_json",),
    "node_desired_pin": (),
    "sync_events": (),
}

# Tables that carry a node_id column and can therefore be filtered per node.
NODE_SCOPED_TABLES = frozenset(
    {
        "bgp_peers",
        "ibgp_peers",
        "managed_nodes",
        "config_proposals",
        "node_reports",
        "config_revisions",
        "node_desired_pin",
        "sync_events",
    }
)


@dataclass(frozen=True)
class TableSummary:
    name: str
    rows: int
    redacted: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TablePage:
    table: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    redacted: list[str] = field(default_factory=list)


def _require_table(table: str) -> tuple[str, tuple[str, ...]]:
    """Look the table up in the allow-list.

    The name returned is the literal from the allow-list, not the requested string,
    and that is the only value ever interpolated into SQL. A miss always raises;
    there is deliberately no fallback that just queries whatever name the user sent.
    """
    for key, redacted in BROWSABLE_TABLES.items():
        if key == table:
            return key, redacted
    raise Dn42CtlError(f"未知或不可浏览的表: {table}")


def _redact_value(value: Any) -> Any:
    """Map a non-NULL value to "***" and NULL to None.

    Never emit a prefix of the real value: a token digest prefix makes offline matching
    feasible, and a WireGuard private key prefix is a genuine reduction of the key
    space. Preserving the NULL / non-NULL distinction is intentional — that is exactly
    the has_token semantics the Nodes page already relies on.
    """
    return None if value is None else REDACTED


def _redact_payload(value: Any) -> Any:
    """Payload columns: replace with a placeholder that keeps the size but not the content."""
    if value is None:
        return None
    size = len(value) if isinstance(value, str | bytes) else len(str(value))
    return f"<payload: {size} bytes>"


def list_tables(*, db_path: Path) -> list[TableSummary]:
    db = Database.open(db_path)
    try:
        out: list[TableSummary] = []
        for table, redacted in BROWSABLE_TABLES.items():
            try:
                count = db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 — allow-listed key
            except sqlite3.Error:  # pragma: no cover — migrations guarantee the table exists
                continue
            out.append(TableSummary(name=table, rows=int(count), redacted=list(redacted)))
    finally:
        db.close()
    return out


def browse_table(
    *,
    db_path: Path,
    table: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    node_id: str | None = None,
) -> TablePage:
    name, redacted = _require_table(table)
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    where = ""
    params: tuple[object, ...] = ()
    if node_id and name in NODE_SCOPED_TABLES:
        where = " WHERE node_id=?"
        params = (node_id,)

    db = Database.open(db_path)
    try:
        try:
            total = int(
                db.connection.execute(
                    f"SELECT COUNT(*) FROM {name}{where}",  # noqa: S608 — name comes from an allow-listed key
                    params,
                ).fetchone()[0]
            )
            cur = db.connection.execute(
                f"SELECT * FROM {name}{where} ORDER BY rowid LIMIT ? OFFSET ?",  # noqa: S608 — same as above
                (*params, limit, offset),
            )
            fetched = cur.fetchall()
            columns = [d[0] for d in cur.description]
        except sqlite3.Error as exc:
            raise Dn42CtlError(f"读取表失败: {name}") from exc
    finally:
        db.close()

    rows: list[dict[str, Any]] = []
    for row in fetched:
        item: dict[str, Any] = {}
        for col in columns:
            value = row[col]
            if col in redacted:
                item[col] = _redact_payload(value) if col.endswith("_json") else _redact_value(value)
            else:
                item[col] = value
        rows.append(item)

    return TablePage(
        table=name,
        columns=columns,
        rows=rows,
        total=total,
        limit=limit,
        offset=offset,
        redacted=list(redacted),
    )


def table_page_to_dict(page: TablePage) -> dict[str, Any]:
    return {
        "table": page.table,
        "columns": page.columns,
        "rows": page.rows,
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "redacted": page.redacted,
    }


__all__ = [
    "BROWSABLE_TABLES",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "NODE_SCOPED_TABLES",
    "REDACTED",
    "TablePage",
    "TableSummary",
    "browse_table",
    "list_tables",
    "table_page_to_dict",
]
