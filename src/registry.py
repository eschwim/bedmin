"""ServerRegistry: reads and writes the server registry JSON file."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from src.server_instance import ServerInstance

logger = logging.getLogger(__name__)


class ServerRegistry:
    def __init__(self, registry_path: Path) -> None:
        self._path = registry_path
        self._ensure_config_dir()

    def _ensure_config_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, dict]:
        if not self._path.exists():
            return {}
        with self._path.open() as f:
            return json.load(f)

    def save(self, data: Dict[str, dict]) -> None:
        self._ensure_config_dir()
        with self._path.open("w") as f:
            json.dump(data, f, indent=2)
        logger.debug("Registry saved to %s", self._path)

    def get_server(self, name: str) -> Optional[ServerInstance]:
        data = self.load()
        entry = data.get(name)
        if entry is None:
            return None
        return ServerInstance.from_dict(entry)

    def add_server(self, server: ServerInstance) -> None:
        data = self.load()
        if server.name in data:
            raise ValueError(f"Server '{server.name}' already exists in registry")
        data[server.name] = server.to_dict()
        self.save(data)
        logger.info("Registered server '%s'", server.name)

    def update_server(self, name: str, updates: dict) -> None:
        data = self.load()
        if name not in data:
            raise KeyError(f"Server '{name}' not found in registry")
        data[name].update(updates)
        self.save(data)

    def remove_server(self, name: str) -> None:
        data = self.load()
        if name not in data:
            raise KeyError(f"Server '{name}' not found in registry")
        del data[name]
        self.save(data)
        logger.info("Removed server '%s' from registry", name)

    def list_servers(self) -> List[ServerInstance]:
        data = self.load()
        return [ServerInstance.from_dict(entry) for entry in data.values()]

    def require_server(self, name: str) -> ServerInstance:
        """Get a server or raise a clear error if not found."""
        server = self.get_server(name)
        if server is None:
            raise KeyError(f"No server named '{name}'. Run 'minectl server list' to see available servers.")
        return server
