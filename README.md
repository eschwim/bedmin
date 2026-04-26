# bedmin

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
git clone <repo-url> bedmin
cd bedmin
./setup.sh
```

`setup.sh` creates a virtualenv, installs dependencies, adds `bedmin` and `bedmin-tui` to your `PATH`, and optionally installs the systemd service.

To install manually:

```bash
python -m venv venv
source venv/bin/activate
pip install -e .
```

## Quick Start

```bash
# Start the daemon (required before managing servers)
bedmin daemon start

# Create and start a server
bedmin server create myserver --port 19132
bedmin start myserver

# Check status
bedmin status

# Follow logs
bedmin logs myserver --follow

# Send a console command
bedmin run myserver "list"

# Open the TUI
bedmin-tui
```

## The Daemon

All server lifecycle operations (start, stop, restart, run) go through the daemon. The daemon:

- Keeps servers running via PTY-based stdin so Bedrock's console accepts commands
- Runs scheduled auto-backups and auto-updates
- Exposes a Unix socket (`~/.config/bedmin/daemon.sock`) for CLI and TUI communication

```bash
bedmin daemon start          # Start in background
bedmin daemon stop           # Stop
bedmin daemon status         # Show status and scheduled jobs
bedmin daemon install --start  # Install as systemd user service and start
```

Without the daemon running, `bedmin start`, `stop`, `restart`, and `run` will fail with a clear error.

### systemd

For the daemon to survive reboots and log-outs (with lingering enabled):

```bash
bedmin daemon install --start
```

This writes `~/.config/systemd/user/bedmin.service` and enables it. View logs with:

```bash
journalctl --user -u bedmin -f
```

## CLI Reference

### Server management

```bash
bedmin server create NAME [--port PORT] [--version VER] [--url URL] [--path DIR]
bedmin server list
bedmin server info NAME
bedmin server configure NAME [OPTIONS]   # show or change per-server settings
bedmin server delete NAME [--stop] [--yes] [--keep-files]
```

`server configure` options:

| Flag | Description |
|---|---|
| `--auto-backup / --no-auto-backup` | Enable scheduled backups |
| `--backup-interval HOURS` | Hours between backups (default: 24) |
| `--auto-update / --no-auto-update` | Enable scheduled updates |
| `--update-interval HOURS` | Hours between update checks (default: 168) |
| `--port PORT` | Change the server port |

Advanced backup options (`skip_unchanged_backup`, `retention_daily_days`, etc.) are set by editing `~/.config/bedmin/servers.json` directly — see [Backup retention](#backup-retention) below.

### Lifecycle

```bash
bedmin start NAME
bedmin stop NAME [--force]    # --force sends SIGKILL instead of SIGTERM
bedmin restart NAME
bedmin status [NAME] [--json]
```

### Logs

```bash
bedmin logs NAME              # Show last 50 lines
bedmin logs NAME -n 200       # Show last N lines
bedmin logs NAME --follow     # Stream in real time
bedmin logs NAME --search PATTERN
```

### Console commands

```bash
bedmin run NAME "COMMAND"

# Examples
bedmin run myserver "list"
bedmin run myserver "say Hello everyone"
bedmin run myserver "op PlayerName"
```

The command is forwarded to the server's stdin via the daemon's relay thread and executed immediately. Responses appear in the server log.

### Updates

```bash
bedmin update NAME                      # Update to latest
bedmin update NAME --version 1.21.0.3  # Pin to specific version
bedmin update NAME --backup-first       # Backup before updating
```

The server is stopped, updated, and restarted automatically if it was running.

### Backups

Backups are stored in `<server-dir>/backups/` as timestamped zip files.

```bash
bedmin backup create NAME [--label LABEL]
bedmin backup list NAME
bedmin backup restore NAME BACKUP_FILE [--yes]
bedmin backup delete NAME BACKUP_FILE [--yes]
```

### Backup retention

Retention and deduplication are configured per-server in `~/.config/bedmin/servers.json`.

**Skip unchanged backups** — skip the backup if nothing has changed since the last one (based on file modification times and sizes):

```json
"skip_unchanged_backup": true
```

**Tiered retention** — keep all backups within a daily window, then one per week, then one per month. Backups beyond all windows are pruned automatically after each backup run:

```json
"retention_daily_days": 7,
"retention_weekly_weeks": 4,
"retention_monthly_months": 12
```

Any tier can be omitted or set to `0`. If `retention_daily_days` is `0` (the default), no automatic pruning occurs and backups accumulate until deleted manually.

### Players

```bash
bedmin players online NAME

# Whitelist
bedmin players whitelist add NAME PLAYER [--xuid XUID] [--ignore-limit]
bedmin players whitelist remove NAME PLAYER
bedmin players whitelist list NAME
bedmin players whitelist enable NAME
bedmin players whitelist disable NAME

# Permissions (visitor / member / operator)
bedmin players permissions set NAME PLAYER --xuid XUID --level LEVEL
bedmin players permissions list NAME
```

## TUI

```bash
bedmin-tui
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

All state is stored under `~/.config/bedmin/`:

| Path | Contents |
|---|---|
| `servers.json` | Server registry |
| `daemon.pid` | Daemon process ID |
| `daemon.sock` | IPC socket |
| `logs/bedmin.log` | Daemon and CLI log |
| `cache/` | Cached server zip downloads |

Server files are installed to `~/mc-servers/<name>/` by default (configurable with `--path` at creation time).

### servers.json fields

Most fields are managed by the CLI, but the following are set by editing the file directly:

| Field | Type | Default | Description |
|---|---|---|---|
| `skip_unchanged_backup` | bool | `false` | Skip backup if nothing changed since the last one |
| `retention_daily_days` | int | `0` | Keep all backups created within this many days |
| `retention_weekly_weeks` | int | `0` | Beyond the daily window, keep one backup per week for this many weeks |
| `retention_monthly_months` | int | `0` | Beyond the weekly window, keep one backup per month for this many months |

## Architecture

```
bedmin CLI ──┐
              ├── Unix socket (JSON) ──▶ Daemon
bedmin-tui ──┘                            │
                                           ├── ProcessManager (start/stop/status)
                                           ├── Relay threads (FIFO → PTY master)
                                           ├── Backup scheduler
                                           └── Update scheduler
```

The daemon is the sole owner of running server processes. It starts each server with a PTY as stdin (so Bedrock's console command reader activates) and runs a relay thread per server that forwards writes to a named FIFO through to the PTY master. The CLI and TUI write commands to the FIFO via the daemon's IPC socket.
