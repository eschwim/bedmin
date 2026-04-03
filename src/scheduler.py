"""Daemon scheduler: runs automatic backups and updates, manages server processes,
and serves an IPC socket for CLI/TUI commands."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import schedule

import config
from src.backup_manager import BackupManager
from src.downloader import BedrockDownloader
from src.process_manager import ProcessManager
from src.registry import ServerRegistry
from src.server_instance import ServerInstance

logger = logging.getLogger(__name__)

DAEMON_PID_FILE = config.CONFIG_DIR / "daemon.pid"
CONFIG_RELOAD_INTERVAL = 300  # re-read server registry every 5 minutes


class DaemonScheduler:
    """
    Runs in the foreground, managing server processes and scheduling backups/updates.
    Exposes a Unix socket at config.DAEMON_SOCKET for IPC with the CLI and TUI.
    Re-reads the registry every CONFIG_RELOAD_INTERVAL seconds to pick up changes.
    """

    def __init__(self) -> None:
        self._registry = ServerRegistry(config.REGISTRY_FILE)
        self._pm = ProcessManager()
        self._bm = BackupManager()
        self._stop = False
        # Maps server name → open PTY master fd (owned by daemon, used by relay threads)
        self._master_fds: Dict[str, int] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """Main loop. Runs until SIGTERM or SIGINT. Writes a PID file on start."""
        self._write_pid()
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info("minectl daemon started (PID %d)", os.getpid())
        self._reload_jobs()
        self._restart_stopped_servers()

        # IPC server runs in a daemon thread so it doesn't block the scheduler loop
        ipc_thread = threading.Thread(
            target=self._run_ipc_server, daemon=True, name="ipc-server"
        )
        ipc_thread.start()

        schedule.every(CONFIG_RELOAD_INTERVAL).seconds.do(self._reload_jobs)

        try:
            while not self._stop:
                schedule.run_pending()
                time.sleep(10)
        finally:
            schedule.clear()
            self._clear_pid()
            logger.info("minectl daemon stopped")

    # -------------------------------------------------------------------------
    # Job management
    # -------------------------------------------------------------------------

    def _reload_jobs(self) -> None:
        """Clear all scheduled jobs and re-register them from current server configs."""
        schedule.clear("server")

        servers = self._registry.list_servers()
        if not servers:
            logger.info("No servers registered; nothing to schedule.")
            return

        count_backup = count_update = 0
        for server in servers:
            if server.auto_backup:
                schedule.every(server.backup_interval_hours).hours.do(
                    self._run_backup, server.name
                ).tag("server")
                count_backup += 1
                logger.info(
                    "Scheduled backup for '%s' every %dh",
                    server.name, server.backup_interval_hours,
                )
            if server.auto_update:
                schedule.every(server.update_interval_hours).hours.do(
                    self._run_update, server.name
                ).tag("server")
                count_update += 1
                logger.info(
                    "Scheduled update for '%s' every %dh",
                    server.name, server.update_interval_hours,
                )

        logger.info(
            "Jobs loaded: %d backup, %d update across %d server(s)",
            count_backup, count_update, len(servers),
        )

    def _restart_stopped_servers(self) -> None:
        """Start any registered servers that are not currently running."""
        servers = self._registry.list_servers()
        for server in servers:
            if not self._pm.is_running(server):
                logger.info("Starting '%s' (not running at daemon startup)...", server.name)
                try:
                    pid = self._launch_server(server.name)
                    logger.info("Started '%s' (PID %d)", server.name, pid)
                except Exception as exc:
                    logger.error("Failed to start '%s': %s", server.name, exc)

    # -------------------------------------------------------------------------
    # Server lifecycle helpers (used internally and by IPC handlers)
    # -------------------------------------------------------------------------

    def _launch_server(self, server_name: str) -> int:
        """Start server, store master_fd, launch relay thread. Returns PID."""
        server = self._registry.get_server(server_name)
        if server is None:
            raise KeyError(f"Server '{server_name}' not registered")
        pid, master_fd = self._pm.start(server)
        with self._lock:
            old = self._master_fds.pop(server_name, None)
            if old is not None:
                try:
                    os.close(old)
                except OSError:
                    pass
            self._master_fds[server_name] = master_fd
        t = threading.Thread(
            target=self._relay_loop,
            args=(server_name, server.stdin_fifo, master_fd, pid),
            daemon=True,
            name=f"relay-{server_name}",
        )
        t.start()
        return pid

    def _relay_loop(
        self, server_name: str, fifo_path: Path, master_fd: int, server_pid: int
    ) -> None:
        """Thread: forwards FIFO writes to the PTY master until the server exits."""
        while Path(f"/proc/{server_pid}").exists():
            try:
                with fifo_path.open("r") as f:
                    data = f.read()
                if data:
                    os.write(master_fd, data.encode())
            except OSError:
                time.sleep(0.1)
        # Server exited — clean up our reference to this fd
        with self._lock:
            if self._master_fds.get(server_name) == master_fd:
                self._master_fds.pop(server_name, None)
        try:
            os.close(master_fd)
        except OSError:
            pass
        logger.info("Relay thread for '%s' exited", server_name)

    # -------------------------------------------------------------------------
    # IPC server
    # -------------------------------------------------------------------------

    def _run_ipc_server(self) -> None:
        """Listen on DAEMON_SOCKET; spawn a handler thread per connection."""
        sock_path = config.DAEMON_SOCKET
        sock_path.unlink(missing_ok=True)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind(str(sock_path))
                srv.listen(5)
                srv.settimeout(1.0)
                logger.info("IPC socket listening at %s", sock_path)
                while not self._stop:
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    threading.Thread(
                        target=self._handle_ipc_conn,
                        args=(conn,),
                        daemon=True,
                    ).start()
        finally:
            sock_path.unlink(missing_ok=True)

    def _handle_ipc_conn(self, conn: socket.socket) -> None:
        try:
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in buf:
                        break
                req = json.loads(buf.decode().strip())
                resp = self._dispatch_ipc(req)
                conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception as exc:
            logger.error("IPC handler error: %s", exc)
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
            except Exception:
                pass

    def _dispatch_ipc(self, req: dict) -> dict:
        action = req.get("action", "")
        name = req.get("server", "")
        try:
            if action == "start":
                pid = self._launch_server(name)
                return {"ok": True, "pid": pid}
            elif action == "stop":
                server = self._registry.require_server(name)
                self._pm.stop(server, graceful=req.get("graceful", True))
                return {"ok": True}
            elif action == "restart":
                server = self._registry.require_server(name)
                self._pm.stop(server)
                pid = self._launch_server(name)
                return {"ok": True, "pid": pid}
            elif action == "run":
                server = self._registry.require_server(name)
                self._pm.send_command(server, req.get("cmd", ""))
                return {"ok": True}
            else:
                return {"ok": False, "error": f"Unknown action: {action!r}"}
        except (KeyError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

    # -------------------------------------------------------------------------
    # Scheduled job runners
    # -------------------------------------------------------------------------

    def _run_backup(self, server_name: str) -> None:
        server = self._registry.get_server(server_name)
        if server is None:
            logger.warning("Backup skipped: server '%s' no longer registered", server_name)
            return

        logger.info("Auto-backup starting for '%s'...", server_name)
        try:
            path = self._bm.create(server, label="auto")
            logger.info("Auto-backup complete: %s", path.name)
        except Exception as exc:
            logger.error("Auto-backup failed for '%s': %s", server_name, exc)

    def _run_update(self, server_name: str) -> None:
        server = self._registry.get_server(server_name)
        if server is None:
            logger.warning("Update skipped: server '%s' no longer registered", server_name)
            return

        logger.info("Auto-update check for '%s' (current: %s)...", server_name, server.version)
        try:
            downloader = BedrockDownloader(config.CACHE_DIR)
            version, url = downloader.get_latest_version_url()

            if version == server.version:
                logger.info("'%s' is already up to date (%s)", server_name, version)
                return

            logger.info("Updating '%s': %s → %s", server_name, server.version, version)

            try:
                bp = self._bm.create(server, label="pre-update")
                logger.info("Pre-update backup created: %s", bp.name)
            except Exception as exc:
                logger.warning("Pre-update backup failed (continuing anyway): %s", exc)

            was_running = self._pm.is_running(server)
            if was_running:
                logger.info("Stopping '%s' for update...", server_name)
                self._pm.stop(server)

            downloader.download(url, version, server.path, force=True)
            self._registry.update_server(server_name, {
                "version": version,
                "updated_at": datetime.now().isoformat(),
            })
            logger.info("Updated '%s' to %s", server_name, version)

            if was_running:
                logger.info("Restarting '%s'...", server_name)
                pid = self._launch_server(server_name)
                logger.info("'%s' restarted (PID %d)", server_name, pid)

        except Exception as exc:
            logger.error("Auto-update failed for '%s': %s", server_name, exc)

    # -------------------------------------------------------------------------
    # Signal handling + PID file
    # -------------------------------------------------------------------------

    def _handle_signal(self, signum: int, frame) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        self._stop = True

    def _write_pid(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DAEMON_PID_FILE.write_text(str(os.getpid()))

    def _clear_pid(self) -> None:
        DAEMON_PID_FILE.unlink(missing_ok=True)


# -------------------------------------------------------------------------
# Helpers for CLI commands
# -------------------------------------------------------------------------

def get_daemon_pid() -> Optional[int]:
    """Return the daemon PID if it's running, else None."""
    if not DAEMON_PID_FILE.exists():
        return None
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if Path(f"/proc/{pid}").exists() else None
