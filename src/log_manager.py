"""LogManager: tail, follow, and search server logs with color highlighting."""

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import List

from src.server_instance import ServerInstance

logger = logging.getLogger(__name__)

# ANSI color codes for log line colorization
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"

_PLAYER_EVENTS = re.compile(r"Player (connected|disconnected):", re.IGNORECASE)
_WARNING = re.compile(r"\[WARNING\]", re.IGNORECASE)
_ERROR = re.compile(r"\[ERROR\]", re.IGNORECASE)


class LogManager:
    def tail(self, server: ServerInstance, lines: int = 50) -> None:
        """Print the last N lines of the server log."""
        if not server.log_file.exists():
            logger.warning("No log file found at %s", server.log_file)
            return
        result = subprocess.run(
            ["tail", "-n", str(lines), str(server.log_file)],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            print(self._colorize_line(line))

    def follow(self, server: ServerInstance) -> None:
        """Stream new log lines to stdout in real time. Ctrl-C to stop."""
        if not server.log_file.exists():
            logger.warning("No log file found at %s — server may not have started yet", server.log_file)
            return

        proc = subprocess.Popen(
            ["tail", "-f", "-n", "0", str(server.log_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            print(f"Following logs for '{server.name}' — press Ctrl-C to stop\n")
            for line in proc.stdout:
                print(self._colorize_line(line.rstrip()))
        except KeyboardInterrupt:
            print("\nStopped following logs.")
        finally:
            proc.terminate()

    def search(self, server: ServerInstance, pattern: str, last_n_lines: int = 0) -> List[str]:
        """Return lines matching pattern from the server log."""
        if not server.log_file.exists():
            return []

        if last_n_lines > 0:
            result = subprocess.run(
                ["tail", "-n", str(last_n_lines), str(server.log_file)],
                capture_output=True,
                text=True,
            )
            lines = result.stdout.splitlines()
        else:
            lines = server.log_file.read_text().splitlines()

        compiled = re.compile(pattern, re.IGNORECASE)
        return [line for line in lines if compiled.search(line)]

    def _colorize_line(self, line: str) -> str:
        if _ERROR.search(line):
            return f"{_RED}{line}{_RESET}"
        if _WARNING.search(line):
            return f"{_YELLOW}{line}{_RESET}"
        if _PLAYER_EVENTS.search(line):
            return f"{_CYAN}{line}{_RESET}"
        return line
