"""BackupManager: zip-based backup and restore for server worlds and configs."""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

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
    def create(self, server: ServerInstance, label: str = "", force: bool = False) -> Path | None:
        """
        Create a zip backup of the server's world and config files.
        Returns the path to the created zip, or None if skipped (no changes).
        Prunes old backups afterward according to the server's retention policy.
        Pass force=True to bypass skip-if-unchanged (used for pre-update backups).
        """
        server.backup_dir.mkdir(parents=True, exist_ok=True)

        fingerprint = ""
        if server.skip_unchanged_backup:
            fingerprint = self._compute_fingerprint(server)
            if not force:
                recent = self.list_backups(server)
                if recent and recent[0].get("content_fingerprint") == fingerprint:
                    logger.info(
                        "Skipping backup for '%s': no changes since last backup", server.name
                    )
                    return None

        timestamp = datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
        filename = f"{server.name}_{timestamp}"
        if label:
            filename += f"_{label}"
        filename += ".zip"
        zip_path = server.backup_dir / filename

        included: list[str] = []
        all_files: list[Path] = []
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
            "content_fingerprint": fingerprint,
        }

        logger.info("Creating backup: %s", zip_path)
        with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
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

    def list_backups(self, server: ServerInstance) -> list[dict]:
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
        if server.retention_daily_days > 0:
            self._apply_tiered_retention(server)

    def _apply_tiered_retention(self, server: ServerInstance) -> None:
        """RRD-style bucketed pruning: daily window, then weekly, then monthly."""
        backups = self.list_backups(server)
        now = datetime.now()

        daily_cutoff = now - timedelta(days=server.retention_daily_days)
        weekly_cutoff = daily_cutoff - timedelta(weeks=server.retention_weekly_weeks)
        monthly_cutoff = weekly_cutoff - timedelta(days=server.retention_monthly_months * 30)

        to_keep: set[str] = set()
        seen_weeks: set[tuple] = set()
        seen_months: set[tuple] = set()

        for b in backups:  # newest-first
            if not b["created_at"]:
                to_keep.add(b["filename"])  # unparseable timestamp — keep defensively
                continue
            dt = datetime.fromisoformat(b["created_at"])

            if dt >= daily_cutoff:
                to_keep.add(b["filename"])
            elif dt >= weekly_cutoff:
                key = dt.isocalendar()[:2]  # (year, week_number)
                if key not in seen_weeks:
                    seen_weeks.add(key)
                    to_keep.add(b["filename"])
            elif dt >= monthly_cutoff:
                key = (dt.year, dt.month)
                if key not in seen_months:
                    seen_months.add(key)
                    to_keep.add(b["filename"])
            # beyond all windows: let it be pruned

        for b in backups:
            if b["filename"] not in to_keep:
                logger.info("Pruning backup (tiered retention): %s", b["filename"])
                Path(b["path"]).unlink(missing_ok=True)

    def _compute_fingerprint(self, server: ServerInstance) -> str:
        """SHA-256 of sorted 'relpath:mtime_ns:size' entries across all backup targets."""
        h = hashlib.sha256()
        entries: list[str] = []
        for target_name in _BACKUP_TARGETS:
            target = server.path / target_name
            if target.is_dir():
                for f in sorted(target.rglob("*")):
                    if f.is_file():
                        st = f.stat()
                        entries.append(f"{f.relative_to(server.path)}:{st.st_mtime_ns}:{st.st_size}")
            elif target.is_file():
                st = target.stat()
                entries.append(f"{target.name}:{st.st_mtime_ns}:{st.st_size}")
        for entry in entries:
            h.update(entry.encode())
        return h.hexdigest()

    def _read_backup_info(self, zip_path: Path) -> dict:
        size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
        info = {
            "filename": zip_path.name,
            "path": str(zip_path),
            "size_mb": size_mb,
            "created_at": "",
            "label": "",
            "version": "",
            "content_fingerprint": "",
        }
        try:
            with ZipFile(zip_path) as zf:
                if BACKUP_METADATA_NAME in zf.namelist():
                    meta = json.loads(zf.read(BACKUP_METADATA_NAME))
                    info["created_at"] = meta.get("created_at", "")
                    info["label"] = meta.get("label", "")
                    info["version"] = meta.get("server_version", "")
                    info["content_fingerprint"] = meta.get("content_fingerprint", "")
        except Exception:
            pass
        return info
