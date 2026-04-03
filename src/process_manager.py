"""ProcessManager: start, stop, and send commands to Bedrock server processes."""

import contextlib
import logging
import os
import pty
import signal
import subprocess
import time
from pathlib import Path

from config import (
    SHUTDOWN_TIMEOUT,
    STARTUP_POLL_INTERVAL,
    STARTUP_TIMEOUT,
)
from src.server_instance import ServerInstance

logger = logging.getLogger(__name__)


class ProcessManager:
    def start(self, server: ServerInstance) -> tuple[int, int]:
        """
        Launch the server process under a PTY.
        Returns (pid, master_fd). The caller owns master_fd and must close it
        when the server stops (the daemon holds it open for the relay thread).
        Raises RuntimeError if already running or startup times out.
        """
        if self.is_running(server):
            raise RuntimeError(f"Server '{server.name}' is already running (PID {self.get_pid(server)})")

        server.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._create_fifo(server)

        binary = server.path / "bedrock_server"
        if not binary.exists():
            raise RuntimeError(f"Server binary not found at {binary}")

        log_fd = server.log_file.open("a")

        # Use a PTY so Bedrock enables its console command reader (it checks isatty)
        master_fd, slave_fd = pty.openpty()

        proc = subprocess.Popen(
            [str(binary)],
            cwd=str(server.path),
            stdin=slave_fd,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
        )
        os.close(slave_fd)
        log_fd.close()

        self._write_pid(server, proc.pid)
        logger.info("Started server '%s' (PID %d)", server.name, proc.pid)

        if not self._wait_for_startup(server):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            self._clear_pid(server)
            with contextlib.suppress(OSError):
                os.close(master_fd)
            raise RuntimeError(
                f"Server '{server.name}' did not report 'Server started' within {STARTUP_TIMEOUT}s. "
                f"Check logs: {server.log_file}"
            )

        return proc.pid, master_fd

    def stop(self, server: ServerInstance, graceful: bool = True) -> None:
        """Send SIGTERM (graceful) or SIGKILL to stop the server. Falls back to SIGKILL."""
        pid = self.get_pid(server)
        if pid is None:
            logger.warning("Server '%s' is not running", server.name)
            return

        sig = signal.SIGTERM if graceful else signal.SIGKILL
        try:
            os.kill(pid, sig)
            logger.info("Sent %s to '%s' (PID %d)", sig.name, server.name, pid)
        except ProcessLookupError:
            pass

        # Wait for process to exit
        deadline = time.time() + SHUTDOWN_TIMEOUT
        while time.time() < deadline:
            if not self._pid_exists(pid):
                break
            time.sleep(0.5)
        else:
            logger.warning("Server '%s' did not stop in %ds; sending SIGKILL", server.name, SHUTDOWN_TIMEOUT)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

        self._clear_pid(server)
        self._remove_fifo(server)
        logger.info("Server '%s' stopped", server.name)

    def status(self, server: ServerInstance) -> dict:
        """Return runtime status dict."""
        pid = self.get_pid(server)
        if pid is None:
            return {"running": False, "pid": None, "uptime_seconds": None, "memory_mb": None}

        uptime = self._get_uptime(pid)
        memory = self._get_memory_mb(pid)
        return {
            "running": True,
            "pid": pid,
            "uptime_seconds": uptime,
            "memory_mb": memory,
        }

    def send_command(self, server: ServerInstance, command: str) -> None:
        """Write a command + newline to the server's stdin FIFO (relay thread forwards to PTY)."""
        if not self.is_running(server):
            raise RuntimeError(f"Server '{server.name}' is not running")
        if not server.stdin_fifo.exists():
            raise RuntimeError(f"FIFO not found at {server.stdin_fifo}")
        with server.stdin_fifo.open("w") as f:
            f.write(command + "\n")
        logger.debug("Sent command to '%s': %s", server.name, command)

    def get_pid(self, server: ServerInstance) -> int | None:
        if not server.pid_file.exists():
            return None
        try:
            pid = int(server.pid_file.read_text().strip())
        except (ValueError, OSError):
            return None
        return pid if self._pid_exists(pid) else None

    def is_running(self, server: ServerInstance) -> bool:
        return self.get_pid(server) is not None

    # --- Private ---

    def _create_fifo(self, server: ServerInstance) -> None:
        if server.stdin_fifo.exists():
            server.stdin_fifo.unlink()
        os.mkfifo(str(server.stdin_fifo))

    def _remove_fifo(self, server: ServerInstance) -> None:
        if server.stdin_fifo.exists():
            with contextlib.suppress(OSError):
                server.stdin_fifo.unlink()

    def _write_pid(self, server: ServerInstance, pid: int) -> None:
        server.pid_file.write_text(str(pid))

    def _clear_pid(self, server: ServerInstance) -> None:
        if server.pid_file.exists():
            server.pid_file.unlink()

    def _pid_exists(self, pid: int) -> bool:
        return Path(f"/proc/{pid}").exists()

    def _wait_for_startup(self, server: ServerInstance) -> bool:
        """Poll the log file for 'Server started' within STARTUP_TIMEOUT seconds."""
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if server.log_file.exists():
                content = server.log_file.read_text()
                if "Server started" in content:
                    return True
            time.sleep(STARTUP_POLL_INTERVAL)
        return False

    def _get_uptime(self, pid: int) -> int | None:
        try:
            stat_path = Path(f"/proc/{pid}/stat")
            stat_data = stat_path.read_text().split()
            # Field 22 (index 21) is start time in clock ticks since boot
            clk_tck = os.sysconf("SC_CLK_TCK")
            uptime_ticks = int(stat_data[21])
            with open("/proc/uptime") as f:
                system_uptime = float(f.read().split()[0])
            start_seconds = uptime_ticks / clk_tck
            return int(system_uptime - start_seconds)
        except (OSError, IndexError, ValueError):
            return None

    def _get_memory_mb(self, pid: int) -> float | None:
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
        except (OSError, ValueError):
            pass
        return None
