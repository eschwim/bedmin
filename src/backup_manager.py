"""BackupManager: zip-based backup and restore for server worlds and configs."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zipfile import ZipFile, ZIP_DEFLATED

from tqdm import tqdm

from config import (
    BACKUP_METADATA_NAME,
    BACKUP_TIMESTAMP_FORMAT,
)
from src.server_instance import ServerInstance

logger = logging.getLogger(__name__)

# Paths relative to server root that are included in every backup
_BACKUP_TARGETS = ["worlds", "server.properties", "allowlist.json", "permissions.json"]


class BackupManager:
    def create(self, server: ServerInstance, label: str = "") -> Path:
        """
        Create a zip backup of the server's world and config files.
        Returns the path to the created zip file.
        Prunes old backups afterward if count exceeds server.max_backups.
        """
        server.backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
        filename = f"{server.name}_{timestamp}"
        if label:
            filename += f"_{label}"
        filename += ".zip"
        zip_path = server.backup_dir / filename

        included: List[str] = []
        all_files: List[Path] = []
        for target_name in _BACKUP_TARGETS:
            target = server.path / target_name
            if target.is_dir():
                all_files.extend(target.rglob("*"))
                included.append(target_name + "/")
            elif target.is_file():
                all_files.append(target)
                included.append(target_name)

        metadata = {
            "server_name": server.name,
            "server_version": server.version,
            "created_at": datetime.now().isoformat(),
            "label": label,
            "included_paths": included,
        }

        logger.info("Creating backup: %s", zip_path)
        with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
            # Write metadata first
            zf.writestr(BACKUP_METADATA_NAME, json.dumps(metadata, indent=2))
            for file_path in tqdm(all_files, desc="Backing up", unit="file"):
                if file_path.is_file():
                    arcname = str(file_path.relative_to(server.path))
                    zf.write(file_path, arcname)

        size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
        logger.info("Backup created: %s (%.2f MB)", filename, size_mb)

        self._prune_old_backups(server)
        return zip_path

    def restore(self, server: ServerInstance, backup_path: Path) -> None:
        """
        Extract backup zip to the server directory.
        The server must be stopped before calling this.
        """
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found: {backup_path}")

        logger.info("Restoring backup '%s' to '%s'...", backup_path.name, server.path)
        with ZipFile(backup_path) as zf:
            members = [m for m in zf.namelist() if m != BACKUP_METADATA_NAME]
            for member in tqdm(members, desc="Restoring", unit="file"):
                zf.extract(member, server.path)

        logger.info("Restore complete.")

    def list_backups(self, server: ServerInstance) -> List[Dict]:
        """Return list of backup info dicts, sorted newest-first."""
        if not server.backup_dir.exists():
            return []

        backups = []
        for zip_path in server.backup_dir.glob("*.zip"):
            info = self._read_backup_info(zip_path)
            backups.append(info)

        backups.sort(key=lambda b: b["created_at"], reverse=True)
        return backups

    def delete_backup(self, backup_path: Path) -> None:
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found: {backup_path}")
        backup_path.unlink()
        logger.info("Deleted backup: %s", backup_path.name)

    # --- Private ---

    def _prune_old_backups(self, server: ServerInstance) -> None:
        """Delete oldest backups if count exceeds server.max_backups."""
        backups = self.list_backups(server)
        excess = len(backups) - server.max_backups
        if excess > 0:
            for old in backups[-excess:]:  # oldest are at the end (list is newest-first)
                logger.info("Pruning old backup: %s", old["filename"])
                Path(old["path"]).unlink(missing_ok=True)

    def _read_backup_info(self, zip_path: Path) -> Dict:
        size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
        info = {
            "filename": zip_path.name,
            "path": str(zip_path),
            "size_mb": size_mb,
            "created_at": "",
            "label": "",
            "version": "",
        }
        try:
            with ZipFile(zip_path) as zf:
                if BACKUP_METADATA_NAME in zf.namelist():
                    meta = json.loads(zf.read(BACKUP_METADATA_NAME))
                    info["created_at"] = meta.get("created_at", "")
                    info["label"] = meta.get("label", "")
                    info["version"] = meta.get("server_version", "")
        except Exception:
            pass
        return info
