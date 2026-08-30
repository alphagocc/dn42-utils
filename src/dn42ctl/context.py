from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dn42ctl.config import AppConfig, ConfigError, load_config
from dn42ctl.db import Database


@dataclass
class AppContext:
    config_path: Path
    db_path: Path
    _config: AppConfig | None = None
    _db: Database | None = None

    def load_config_optional(self) -> AppConfig | None:
        if self._config is None and self.config_path.exists():
            self._config = load_config(self.config_path)
        return self._config

    def require_config(self) -> AppConfig:
        cfg = self.load_config_optional()
        if cfg is None:
            raise ConfigError("未初始化，请先运行 dn42ctl init")
        return cfg

    def open_db(self) -> Database:
        if self._db is None:
            self._db = Database.open(self.db_path)
        return self._db
