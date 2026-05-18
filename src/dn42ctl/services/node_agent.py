"""Spoke-side node agent: pull / apply / once.

Push / scan / report belong to stage 3+.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dn42ctl.constants import FILE_MODE_PRIVATE
from dn42ctl.fs import chmod_best_effort
from dn42ctl.node_client import NodeClient, NodeClientError
from dn42ctl.node_config import NodeConfig
from dn42ctl.services.core import Dn42CtlError

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cached_desired (
    id INTEGER PRIMARY KEY CHECK (id=1),
    revision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def _open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CACHE_SCHEMA)
    chmod_best_effort(path, FILE_MODE_PRIVATE)
    return conn


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class PullResult:
    revision: str
    fetched_at: str
    payload: dict[str, Any]


def pull(*, node_config: NodeConfig) -> PullResult:
    """Fetch desired state from server and persist into local cache."""
    client = NodeClient(
        server=node_config.server,
        node_id=node_config.node_id,
        token=node_config.token,
    )
    try:
        payload = client.pull_desired()
    except NodeClientError as exc:
        raise Dn42CtlError(str(exc)) from exc

    revision = payload.get("revision")
    if not isinstance(revision, str) or not revision:
        raise Dn42CtlError("server 返回的 desired state 缺少 revision")

    fetched_at = _now_iso()
    conn = _open_cache(node_config.cache_db_path)
    try:
        conn.execute(
            """
            INSERT INTO cached_desired(id, revision, payload_json, fetched_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                revision=excluded.revision,
                payload_json=excluded.payload_json,
                fetched_at=excluded.fetched_at
            """,
            (revision, json.dumps(payload, ensure_ascii=False), fetched_at),
        )
        conn.commit()
    finally:
        conn.close()

    return PullResult(revision=revision, fetched_at=fetched_at, payload=payload)


def read_cache(*, node_config: NodeConfig) -> PullResult | None:
    path = node_config.cache_db_path
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT revision, payload_json, fetched_at FROM cached_desired WHERE id=1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return PullResult(
        revision=row["revision"],
        fetched_at=row["fetched_at"],
        payload=json.loads(row["payload_json"]),
    )
