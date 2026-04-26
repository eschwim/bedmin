"""Central configuration for bedmin. All constants and default values live here."""

from pathlib import Path

# --- Paths ---
CONFIG_DIR = Path.home() / ".config" / "bedmin"
REGISTRY_FILE = CONFIG_DIR / "servers.json"
DEFAULT_SERVERS_DIR = Path.home() / "mc-servers"
CACHE_DIR = CONFIG_DIR / "cache"
LOG_DIR = CONFIG_DIR / "logs"
MANAGER_LOG_FILE = LOG_DIR / "bedmin.log"
DAEMON_SOCKET = CONFIG_DIR / "daemon.sock"

# --- Mojang Download ---
MOJANG_DOWNLOAD_PAGE = "https://www.minecraft.net/en-us/download/server/bedrock"
BEDROCK_URL_PATTERN = r"https://www\.minecraft\.net/bedrockdedicatedserver/bin-linux/bedrock-server-[\d.]+\.zip"
DOWNLOAD_TIMEOUT = 120  # seconds
DOWNLOAD_CHUNK_SIZE = 8192
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# --- Per-Server Filenames ---
PID_FILENAME = "server.pid"
STDIN_FIFO_NAME = "manager.stdin.fifo"
SERVER_LOG_NAME = "logs/server.log"
VERSION_FILE_NAME = "version.txt"
BACKUP_DIR_NAME = "backups"
BACKUP_METADATA_NAME = "backup_metadata.json"
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

# --- Process Management ---
STARTUP_TIMEOUT = 30    # seconds to wait for "Server started" in logs
SHUTDOWN_TIMEOUT = 15   # seconds to wait for clean stop before SIGKILL
STARTUP_POLL_INTERVAL = 0.5  # seconds between log checks during startup

# --- Defaults ---
DEFAULT_PORT = 19132
MAX_BACKUPS_DEFAULT = 10
LOG_TAIL_LINES = 50

# --- Network / Retry ---
MAX_RETRIES = 3
RETRY_BACKOFF = 5  # seconds (doubles each retry)
REQUEST_TIMEOUT = 60  # seconds for non-download requests

# --- Logging ---
LOG_LEVEL = "INFO"
