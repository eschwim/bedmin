"""PlayerManager: whitelist, permissions, and online player tracking via log parsing."""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from src.server_instance import ServerInstance

logger = logging.getLogger(__name__)

# Bedrock log patterns for player events
_CONNECTED = re.compile(r"Player connected:\s+([^,]+),\s+xuid:\s+(\S+)")
_DISCONNECTED = re.compile(r"Player disconnected:\s+([^,]+),\s+xuid:\s+(\S+)")

VALID_PERMISSION_LEVELS = {"visitor", "member", "operator"}


class PlayerManager:
    # --- Whitelist ---

    def whitelist_add(
        self,
        server: ServerInstance,
        player_name: str,
        xuid: str = "",
        ignores_player_limit: bool = False,
    ) -> None:
        entries = self._read_json(server.whitelist_file)
        if any(e.get("name", "").lower() == player_name.lower() for e in entries):
            raise ValueError(f"'{player_name}' is already on the whitelist")
        entries.append({
            "ignoresPlayerLimit": ignores_player_limit,
            "name": player_name,
            "xuid": xuid,
        })
        self._write_json(server.whitelist_file, entries)
        logger.info("Added '%s' to whitelist for server '%s'", player_name, server.name)

    def whitelist_remove(self, server: ServerInstance, player_name: str) -> None:
        entries = self._read_json(server.whitelist_file)
        original_len = len(entries)
        entries = [e for e in entries if e.get("name", "").lower() != player_name.lower()]
        if len(entries) == original_len:
            raise ValueError(f"'{player_name}' not found on whitelist")
        self._write_json(server.whitelist_file, entries)
        logger.info("Removed '%s' from whitelist for server '%s'", player_name, server.name)

    def whitelist_list(self, server: ServerInstance) -> List[Dict]:
        return self._read_json(server.whitelist_file)

    def whitelist_enable(self, server: ServerInstance) -> None:
        self._set_server_property(server, "allow-list", "true")

    def whitelist_disable(self, server: ServerInstance) -> None:
        self._set_server_property(server, "allow-list", "false")

    # --- Permissions ---

    def permissions_set(
        self, server: ServerInstance, player_name: str, xuid: str, level: str
    ) -> None:
        if level not in VALID_PERMISSION_LEVELS:
            raise ValueError(
                f"Invalid permission level '{level}'. Valid: {', '.join(sorted(VALID_PERMISSION_LEVELS))}"
            )
        if not xuid:
            raise ValueError("xuid is required to set permissions (Bedrock uses xuid, not username)")

        entries = self._read_json(server.permissions_file)
        # Update existing entry or add new one
        for entry in entries:
            if entry.get("xuid") == xuid:
                entry["permission"] = level
                self._write_json(server.permissions_file, entries)
                logger.info("Updated permissions for xuid %s (%s) to '%s'", xuid, player_name, level)
                return

        entries.append({"permission": level, "xuid": xuid})
        self._write_json(server.permissions_file, entries)
        logger.info("Set permissions for xuid %s (%s) to '%s'", xuid, player_name, level)

    def permissions_list(self, server: ServerInstance) -> List[Dict]:
        return self._read_json(server.permissions_file)

    # --- Online Players (log parsing) ---

    def get_online_players(self, server: ServerInstance) -> List[str]:
        """
        Parse the server log to determine which players are currently online.
        Scans the entire log and tracks connect/disconnect events.
        """
        if not server.log_file.exists():
            return []

        online: Dict[str, str] = {}  # xuid -> name
        for line in server.log_file.read_text().splitlines():
            match = _CONNECTED.search(line)
            if match:
                online[match.group(2)] = match.group(1).strip()
                continue
            match = _DISCONNECTED.search(line)
            if match:
                online.pop(match.group(2), None)

        return list(online.values())

    # --- Private Helpers ---

    def _read_json(self, path: Path) -> List[Dict]:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return []

    def _write_json(self, path: Path, data: List[Dict]) -> None:
        path.write_text(json.dumps(data, indent=2))

    def _set_server_property(self, server: ServerInstance, key: str, value: str) -> None:
        """Update a key=value line in server.properties."""
        props_path = server.server_properties
        if not props_path.exists():
            logger.warning("server.properties not found at %s", props_path)
            return
        lines = props_path.read_text().splitlines()
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                updated = True
                break
        if not updated:
            lines.append(f"{key}={value}")
        props_path.write_text("\n".join(lines) + "\n")
        logger.info("Set '%s=%s' in server.properties", key, value)
