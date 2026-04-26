"""IPC client: sends requests to the running bedmin daemon via Unix socket."""
from __future__ import annotations

import json
import socket
from typing import Any

import config

SOCKET_TIMEOUT = 60.0  # generous: start can take up to STARTUP_TIMEOUT seconds


def request(action: str, **kwargs: Any) -> dict[str, Any]:
    """Send a request to the daemon and return the parsed response.

    Raises RuntimeError if the daemon is not running or the action fails.
    """
    payload = {"action": action, **kwargs}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(SOCKET_TIMEOUT)
            s.connect(str(config.DAEMON_SOCKET))
            s.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        resp = json.loads(buf.decode().strip())
    except FileNotFoundError:
        raise RuntimeError("Daemon is not running. Start it with: bedmin daemon start") from None
    except (ConnectionRefusedError, OSError) as exc:
        raise RuntimeError(f"Cannot connect to daemon: {exc}") from exc

    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "Unknown daemon error"))
    return resp
