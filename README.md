# minectl

A command-line manager for [Minecraft Bedrock Dedicated Server](https://www.minecraft.net/en-us/download/server/bedrock) on Linux. Handles downloading, running, updating, backing up, and scheduling servers, with both a CLI and an interactive TUI.

## Features

- Download and install any Bedrock server version automatically
- Start/stop/restart servers with graceful shutdown
- Send console commands to running servers
- Live log streaming with colour-coded output
- Scheduled auto-backup and auto-update via a background daemon
- Whitelist and permission management
- Interactive TUI with server overview, logs, players, backups, and properties editor
- systemd user service integration

## Requirements

- Linux
- Python 3.11+
- Internet access (for downloading server binaries from Mojang)

## Installation

```bash
git clone <repo-url> minectl
cd minectl
./setup.sh
```

`setup.sh` creates a virtualenv, installs dependencies, adds `minectl` and `minectl-tui` to your `PATH`, and optionally installs the systemd service.

To install manually:

```bash
python -m venv venv
source venv/bin/activate
pip install -e .
```

## Quick Start

```bash
# Start the daemon (required before managing servers)
minectl daemon start

# Create and start a server
minectl server create myserver --port 19132
minectl start myserver

# Check status
minectl status

# Follow logs
minectl logs myserver --follow

# Send a console command
minectl run myserver "list"

# Open the TUI
minectl-tui
```

## The Daemon

All server lifecycle operations (start, stop, restart, run) go through the daemon. The daemon:

- Keeps servers running via PTY-based stdin so Bedrock's console accepts commands
- Runs scheduled auto-backups and auto-updates
- Exposes a Unix socket (`~/.config/minectl/daemon.sock`) for CLI and TUI communication

```bash
minectl daemon start          # Start in background
minectl daemon stop           # Stop
minectl daemon status         # Show status and scheduled jobs
minectl daemon install --start  # Install as systemd user service and start
```

Without the daemon running, `minectl start`, `stop`, `restart`, and `run` will fail with a clear error.

### systemd

For the daemon to survive reboots and log-outs (with lingering enabled):

```bash
minectl daemon install --start
```

This writes `~/.config/systemd/user/minectl.service` and enables it. View logs with:

```bash
journalctl --user -u minectl -f
```

## CLI Reference

### Server management

```bash
minectl server create NAME [--port PORT] [--version VER] [--url URL] [--path DIR]
minectl server list
minectl server info NAME
minectl server configure NAME [OPTIONS]   # show or change per-server settings
minectl server delete NAME [--stop] [--yes] [--keep-files]
```

`server configure` options:

| Flag | Description |
|---|---|
| `--auto-backup / --no-auto-backup` | Enable scheduled backups |
| `--backup-interval HOURS` | Hours between backups (default: 24) |
| `--max-backups N` | Maximum backups to keep (default: 10) |
| `--auto-update / --no-auto-update` | Enable scheduled updates |
| `--update-interval HOURS` | Hours between update checks (default: 168) |
| `--port PORT` | Change the server port |

### Lifecycle

```bash
minectl start NAME
minectl stop NAME [--force]    # --force sends SIGKILL instead of SIGTERM
minectl restart NAME
minectl status [NAME] [--json]
```

### Logs

```bash
minectl logs NAME              # Show last 50 lines
minectl logs NAME -n 200       # Show last N lines
minectl logs NAME --follow     # Stream in real time
minectl logs NAME --search PATTERN
```

### Console commands

```bash
minectl run NAME "COMMAND"

# Examples
minectl run myserver "list"
minectl run myserver "say Hello everyone"
minectl run myserver "op PlayerName"
```

The command is forwarded to the server's stdin via the daemon's relay thread and executed immediately. Responses appear in the server log.

### Updates

```bash
minectl update NAME                      # Update to latest
minectl update NAME --version 1.21.0.3  # Pin to specific version
minectl update NAME --backup-first       # Backup before updating
```

The server is stopped, updated, and restarted automatically if it was running.

### Backups

Backups are stored in `<server-dir>/backups/` as timestamped zip files.

```bash
minectl backup create NAME [--label LABEL]
minectl backup list NAME
minectl backup restore NAME BACKUP_FILE [--yes]
minectl backup delete NAME BACKUP_FILE [--yes]
```

### Players

```bash
minectl players online NAME

# Whitelist
minectl players whitelist add NAME PLAYER [--xuid XUID] [--ignore-limit]
minectl players whitelist remove NAME PLAYER
minectl players whitelist list NAME
minectl players whitelist enable NAME
minectl players whitelist disable NAME

# Permissions (visitor / member / operator)
minectl players permissions set NAME PLAYER --xuid XUID --level LEVEL
minectl players permissions list NAME
```

## TUI

```bash
minectl-tui
```

### Layout

```
┌─────────────┬──────────────────────────────────────────────────────┐
│  Servers    │  Overview  │  Logs  │  Players  │  Backups  │  Props │
│             │                                                        │
│  ● myserver │  [server details and controls]                        │
│  ○ staging  │                                                        │
│             │                                                        │
├─────────────┴────────────────────────────────────────────┬─────────┤
│ > command input                                           │  [Send] │
└───────────────────────────────────────────────────────────┴─────────┘
```

### Keyboard shortcuts

| Key | Action |
|---|---|
| `n` | Create new server |
| `r` | Refresh server list |
| `q` / `ctrl+c` | Quit |
| `tab` / `shift+tab` | Navigate between elements |
| `enter` | Activate focused element |

### Tabs

- **Overview** — status card (running/stopped, uptime, memory, port, version), Start/Stop/Restart/Update/Backup buttons
- **Logs** — live-streaming log with colour-coded errors, warnings, and player events
- **Players** — online players, whitelist management, permissions
- **Backups** — list, create, restore, and delete backups
- **Properties** — edit `server.properties` with smart field types (dropdowns for enums, integer inputs with range validation, free text for everything else)

## Configuration

All state is stored under `~/.config/minectl/`:

| Path | Contents |
|---|---|
| `servers.json` | Server registry |
| `daemon.pid` | Daemon process ID |
| `daemon.sock` | IPC socket |
| `logs/minectl.log` | Daemon and CLI log |
| `cache/` | Cached server zip downloads |

Server files are installed to `~/mc-servers/<name>/` by default (configurable with `--path` at creation time).

## Architecture

```
minectl CLI ──┐
              ├── Unix socket (JSON) ──▶ Daemon
minectl-tui ──┘                            │
                                           ├── ProcessManager (start/stop/status)
                                           ├── Relay threads (FIFO → PTY master)
                                           ├── Backup scheduler
                                           └── Update scheduler
```

The daemon is the sole owner of running server processes. It starts each server with a PTY as stdin (so Bedrock's console command reader activates) and runs a relay thread per server that forwards writes to a named FIFO through to the PTY master. The CLI and TUI write commands to the FIFO via the daemon's IPC socket.
