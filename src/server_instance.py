"""ServerInstance dataclass: typed data model with derived path properties."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import (
    BACKUP_DIR_NAME,
    DEFAULT_PORT,
    MAX_BACKUPS_DEFAULT,
    PID_FILENAME,
    SERVER_LOG_NAME,
    STDIN_FIFO_NAME,
    VERSION_FILE_NAME,
)


@dataclass
class ServerInstance:
    name: str
    path: Path
    version: str
    port: int = DEFAULT_PORT
    created_at: str = ""
    updated_at: str = ""
    auto_backup: bool = False
    backup_interval_hours: int = 24
    max_backups: int = MAX_BACKUPS_DEFAULT
    auto_update: bool = False
    update_interval_hours: int = 168  # weekly

    # --- Serialization ---

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerInstance:
        return cls(
            name=data["name"],
            path=Path(data["path"]),
            version=data["version"],
            port=data.get("port", DEFAULT_PORT),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            auto_backup=data.get("auto_backup", False),
            backup_interval_hours=data.get("backup_interval_hours", 24),
            max_backups=data.get("max_backups", MAX_BACKUPS_DEFAULT),
            auto_update=data.get("auto_update", False),
            update_interval_hours=data.get("update_interval_hours", 168),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "version": self.version,
            "port": self.port,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "auto_backup": self.auto_backup,
            "backup_interval_hours": self.backup_interval_hours,
            "max_backups": self.max_backups,
            "auto_update": self.auto_update,
            "update_interval_hours": self.update_interval_hours,
        }

    # --- Derived paths (no I/O) ---

    @property
    def pid_file(self) -> Path:
        return self.path / PID_FILENAME

    @property
    def stdin_fifo(self) -> Path:
        return self.path / STDIN_FIFO_NAME

    @property
    def log_file(self) -> Path:
        return self.path / SERVER_LOG_NAME

    @property
    def version_file(self) -> Path:
        return self.path / VERSION_FILE_NAME

    @property
    def backup_dir(self) -> Path:
        return self.path / BACKUP_DIR_NAME

    @property
    def whitelist_file(self) -> Path:
        return self.path / "allowlist.json"

    @property
    def permissions_file(self) -> Path:
        return self.path / "permissions.json"

    @property
    def server_properties(self) -> Path:
        return self.path / "server.properties"
