"""serve_bootstrap: idempotent self-node registration on `dn42ctl serve` start.

Flow (see docs/architecture/sync_hub_spoke.md):

  1. migrate (already done by Database.open)
  2. read/create /var/lib/dn42ctl/self_node_id
  3. UPSERT managed_nodes (is_self=1)
  4. ensure /etc/dn42ctl/node.toml has a matching self token
  5. uvicorn (handled by the CLI caller)
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.constants import FILE_MODE_PRIVATE
from dn42ctl.db import Database
from dn42ctl.db_managed import ManagedNodeStore, hash_token
from dn42ctl.fs import chmod_best_effort
from dn42ctl.node_config import NodeConfig, load_node_config, save_node_config


@dataclass(frozen=True)
class SelfRegistrationResult:
    node_id: str
    created_node_id: bool  # True if self_node_id file was newly generated
    upserted_managed_node: bool  # True if managed_nodes row was created or updated
    rotated_token: bool  # True if a fresh self token was generated
    node_toml_path: Path
    seeded_addresses: list[str] = field(default_factory=list)
    # config.toml 的 node_id 与 self 节点 id 分叉。此时 admin 写入的 peer 会落在
    # desired-state 读不到的分区里,必须让管理员看见。
    config_node_id_mismatch: bool = False


def _read_or_create_self_node_id(self_node_id_path: Path) -> tuple[str, bool]:
    if self_node_id_path.exists():
        existing = self_node_id_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing, False
    self_node_id_path.parent.mkdir(parents=True, exist_ok=True)
    new_id = str(uuid.uuid4())
    self_node_id_path.write_text(new_id + "\n", encoding="utf-8")
    chmod_best_effort(self_node_id_path, FILE_MODE_PRIVATE)
    return new_id, True


def _needs_new_token(node_toml_path: Path, expected_node_id: str) -> bool:
    if not node_toml_path.exists():
        return True
    try:
        existing = load_node_config(node_toml_path)
    except Exception:  # noqa: BLE001 — broken toml -> regenerate
        return True
    if existing.node_id != expected_node_id:
        return True
    return not existing.token


def run_self_registration(
    *,
    db_path: Path,
    self_node_id_path: Path,
    node_toml_path: Path,
    server_url: str = "http://[::1]:4242",
    app_config: AppConfig | None = None,
) -> SelfRegistrationResult:
    """Idempotent self-registration sequence. Safe to call on every `serve` start.

    传入 `app_config` 时，还会用本机 config.toml 的 `own_ipv6` / `router_id` 回填 self
    节点的地址列——**仅当该列为 NULL**，永不覆盖。NULL 门控保证不会震荡：首次升级后
    hub 采纳本机正在工作的配置，此后 DB 是权威（agent 按 DB 写 config.toml，而这里只
    在 DB 为空时读 config.toml）。
    """
    self_node_id, created = _read_or_create_self_node_id(self_node_id_path)

    seeded: list[str] = []
    db = Database.open(db_path)
    try:
        store = ManagedNodeStore(db.connection)
        existing_self = store.get_self()
        # upsert_self always writes; we report True for clarity.
        store.upsert_self(self_node_id, name="self")
        upserted = existing_self is None or existing_self.node_id != self_node_id

        if app_config is not None:
            current = store.get(self_node_id)
            if current is not None:
                pending: dict[str, str] = {}
                if current.own_ipv6 is None and app_config.own_ipv6:
                    pending["own_ipv6"] = app_config.own_ipv6
                if current.router_id is None and app_config.router_id:
                    pending["router_id"] = app_config.router_id
                if pending:
                    store.set_addresses(self_node_id, **pending)  # type: ignore[arg-type]
                    seeded = sorted(pending)

        rotated = False
        if _needs_new_token(node_toml_path, self_node_id):
            plaintext = secrets.token_urlsafe(32)
            store.set_token_hash(self_node_id, hash_token(plaintext))
            cfg = NodeConfig(
                server=server_url,
                node_id=self_node_id,
                token=plaintext,
            )
            save_node_config(node_toml_path, cfg)
            rotated = True
    finally:
        db.close()

    return SelfRegistrationResult(
        node_id=self_node_id,
        created_node_id=created,
        upserted_managed_node=upserted,
        rotated_token=rotated,
        node_toml_path=node_toml_path,
        seeded_addresses=seeded,
        config_node_id_mismatch=app_config is not None and app_config.node_id != self_node_id,
    )
