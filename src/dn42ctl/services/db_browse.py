"""数据库只读浏览。

只读。任何权威写入都必须经过 service 层，因为**变更通知必须与业务写入同事务发射**
（`db.emit_sync_event`）——通用 UPDATE 端点会绕过这条不变量，导致行改了、
`sync_events` 没写、节点永远拿着旧配置且没有任何告警。

端点形状、白名单与脱敏规则见 `docs/architecture/db_browse.md`。
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

# 表名白名单。**绝不**从 sqlite_master 派生 —— URL 永远不能命名一张表。
# 值是该表必须脱敏的列。新增表时必须在这里显式分类:让"这张新表里有没有机密"
# 成为一个必须回答的问题。
#
# 判定规则:当且仅当某列存了机密、**且没有任何现有 admin 路由已经明文返回它**时才脱敏。
#   - config_revisions.payload_json 存的是完整 desired-state 快照,内含每一个
#     wg_private_key,而 _revision_to_dict 刻意从不返回它 —— 必须脱敏,漏了等于把
#     全网私钥挂在 web 上。
#   - config_proposals / node_reports 的 payload_json 已被
#     GET /api/admin/nodes/{id}/{proposals,reports} 全量返回,这里再遮一层只是自欺。
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

# 带 node_id 列、可按节点过滤的表。
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
    """白名单查表。

    返回的表名取自**白名单里的字面量**而不是请求字符串,后续 SQL 只拼这个值。
    未命中一律报错,绝不回退到"就按用户给的名字查一下"。
    """
    for key, redacted in BROWSABLE_TABLES.items():
        if key == table:
            return key, redacted
    raise Dn42CtlError(f"未知或不可浏览的表: {table}")


def _redact_value(value: Any) -> Any:
    """非 NULL -> "***",NULL -> None。

    **绝不给前缀**:argon2 前缀会泄露参数,WireGuard 私钥前缀是实打实的密钥空间缩减。
    保留 NULL / 非 NULL 的区分是有意的 —— 那正是 Nodes 页已经在用的 has_token 语义。
    """
    return None if value is None else REDACTED


def _redact_payload(value: Any) -> Any:
    """payload 列:替换成占位符,保留体积信息但不泄露内容。"""
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
                count = db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 — 白名单 key
            except sqlite3.Error:  # pragma: no cover — 表由迁移保证存在
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
                    f"SELECT COUNT(*) FROM {name}{where}",  # noqa: S608 — name 来自白名单 key
                    params,
                ).fetchone()[0]
            )
            cur = db.connection.execute(
                f"SELECT * FROM {name}{where} ORDER BY rowid LIMIT ? OFFSET ?",  # noqa: S608 — 同上
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
