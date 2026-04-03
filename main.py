"""minectl: Minecraft Bedrock server manager CLI."""

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import click

import config
from src import ipc
from src.backup_manager import BackupManager
from src.downloader import BedrockDownloader
from src.log_manager import LogManager
from src.logging_setup import setup_logging
from src.player_manager import PlayerManager
from src.process_manager import ProcessManager
from src.registry import ServerRegistry
from src.server_instance import ServerInstance

# ---------------------------------------------------------------------------
# Shared context helpers
# ---------------------------------------------------------------------------

def _registry() -> ServerRegistry:
    return ServerRegistry(config.REGISTRY_FILE)


def _process_manager() -> ProcessManager:
    return ProcessManager()


def _err(msg: str) -> None:
    click.echo(click.style(f"Error: {msg}", fg="red"), err=True)


def _ok(msg: str) -> None:
    click.echo(click.style(msg, fg="green"))


def _info(msg: str) -> None:
    click.echo(msg)


def _format_uptime(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _require(name: str) -> ServerInstance:
    """Resolve server name to instance or exit with a clear error."""
    try:
        return _registry().require_server(name)
    except KeyError as exc:
        _err(str(exc))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
def cli(debug: bool) -> None:
    """Minecraft Bedrock Dedicated Server manager."""
    level = "DEBUG" if debug else config.LOG_LEVEL
    setup_logging(config.MANAGER_LOG_FILE, level=level)


# ---------------------------------------------------------------------------
# server group
# ---------------------------------------------------------------------------

@cli.group()
def server() -> None:
    """Create and manage server instances."""


@server.command("create")
@click.argument("name")
@click.option("--port", default=config.DEFAULT_PORT, show_default=True, help="UDP port.")
@click.option("--version", "version_pin", default=None, help="Pin to a specific version (default: latest).")
@click.option("--url", "direct_url", default=None, help="Direct download URL (skips Mojang page scrape).")
@click.option("--path", "install_path", default=None, help="Install directory (default: ~/mc-servers/<name>).")
def server_create(name: str, port: int, version_pin: str | None, direct_url: str | None, install_path: str | None) -> None:
    """Download and create a new server instance."""
    reg = _registry()
    if reg.get_server(name):
        _err(f"Server '{name}' already exists.")
        sys.exit(1)

    dest = Path(install_path) if install_path else config.DEFAULT_SERVERS_DIR / name
    downloader = BedrockDownloader(config.CACHE_DIR)

    try:
        if direct_url:
            url = direct_url
            ver_match = re.search(r"bedrock-server-([\d.]+)\.zip", url)
            if not ver_match:
                _err("Could not extract version from URL. Ensure the URL contains 'bedrock-server-X.Y.Z.zip'.")
                sys.exit(1)
            version = ver_match.group(1)
        elif version_pin:
            version = version_pin
            url = f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{version}.zip"
        else:
            version, url = downloader.get_latest_version_url()

        downloader.download(url, version, dest)
    except Exception as exc:
        _err(str(exc))
        sys.exit(1)

    now = datetime.now().isoformat()
    instance = ServerInstance(
        name=name,
        path=dest,
        version=version,
        port=port,
        created_at=now,
        updated_at=now,
    )
    _patch_port(instance, port)

    try:
        reg.add_server(instance)
    except ValueError as exc:
        _err(str(exc))
        sys.exit(1)

    _ok(f"Server '{name}' created at {dest} (version {version}, port {port}).")


@server.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def server_list(as_json: bool) -> None:
    """List all registered servers."""
    servers = _registry().list_servers()
    if not servers:
        _info("No servers registered. Use 'minectl server create NAME' to add one.")
        return

    pm = _process_manager()
    if as_json:
        rows = []
        for s in servers:
            st = pm.status(s)
            rows.append({**s.to_dict(), "running": st["running"], "pid": st["pid"]})
        click.echo(json.dumps(rows, indent=2))
        return

    header = f"{'NAME':<20} {'VERSION':<14} {'PORT':<6} {'STATUS':<10} {'PATH'}"
    click.echo(click.style(header, bold=True))
    click.echo("-" * 80)
    for s in servers:
        st = pm.status(s)
        status_str = click.style("running", fg="green") if st["running"] else click.style("stopped", fg="red")
        click.echo(f"{s.name:<20} {s.version:<14} {s.port:<6} {status_str:<20} {s.path}")


@server.command("info")
@click.argument("name")
def server_info(name: str) -> None:
    """Show detailed info for a server."""
    s = _require(name)
    pm = _process_manager()
    st = pm.status(s)

    click.echo(click.style(f"\n  Server: {s.name}", bold=True))
    click.echo(f"  Path:      {s.path}")
    click.echo(f"  Version:   {s.version}")
    click.echo(f"  Port:      {s.port}")
    click.echo(f"  Created:   {s.created_at}")
    click.echo(f"  Updated:   {s.updated_at}")
    click.echo(f"  Backups:   max {s.max_backups}")

    if st["running"]:
        click.echo(f"\n  Status:    {click.style('RUNNING', fg='green')} (PID {st['pid']})")
        if st["uptime_seconds"] is not None:
            click.echo(f"  Uptime:    {_format_uptime(st['uptime_seconds'])}")
        if st["memory_mb"] is not None:
            click.echo(f"  Memory:    {st['memory_mb']} MB")
    else:
        click.echo(f"\n  Status:    {click.style('STOPPED', fg='red')}")
    click.echo()


@server.command("delete")
@click.argument("name")
@click.option("--stop", "do_stop", is_flag=True, help="Stop the server if running before deleting.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option("--keep-files", is_flag=True, help="Remove from registry but keep files on disk.")
def server_delete(name: str, do_stop: bool, yes: bool, keep_files: bool) -> None:
    """Remove a server from the registry (and optionally delete its files)."""
    reg = _registry()
    s = _require(name)
    pm = _process_manager()

    if pm.is_running(s):
        if do_stop:
            _info(f"Stopping '{name}'...")
            try:
                ipc.request("stop", server=name)
            except RuntimeError as exc:
                _err(str(exc))
                sys.exit(1)
        else:
            _err(f"Server '{name}' is still running. Use --stop to stop it first.")
            sys.exit(1)

    if not yes:
        action = "unregister (keep files)" if keep_files else f"DELETE '{s.path}' and unregister"
        click.confirm(f"Are you sure you want to {action} '{name}'?", abort=True)

    if not keep_files:
        import shutil
        shutil.rmtree(s.path, ignore_errors=True)
        _info(f"Deleted server directory: {s.path}")

    reg.remove_server(name)
    _ok(f"Server '{name}' removed.")


@server.command("configure")
@click.argument("name")
@click.option("--auto-backup/--no-auto-backup", default=None, help="Enable/disable scheduled backups.")
@click.option("--backup-interval", type=int, default=None, metavar="HOURS", help="Hours between auto-backups.")
@click.option("--max-backups", type=int, default=None, help="Maximum number of backups to keep.")
@click.option("--auto-update/--no-auto-update", default=None, help="Enable/disable scheduled updates.")
@click.option("--update-interval", type=int, default=None, metavar="HOURS", help="Hours between update checks.")
@click.option("--port", type=int, default=None, help="Change the server port.")
def server_configure(
    name: str,
    auto_backup: bool | None,
    backup_interval: int | None,
    max_backups: int | None,
    auto_update: bool | None,
    update_interval: int | None,
    port: int | None,
) -> None:
    """Configure scheduled backup/update settings for a server. Run with no flags to show current config."""
    reg = _registry()
    s = _require(name)

    updates = {}
    if auto_backup is not None:
        updates["auto_backup"] = auto_backup
    if backup_interval is not None:
        if backup_interval < 1:
            _err("--backup-interval must be at least 1 hour.")
            sys.exit(1)
        updates["backup_interval_hours"] = backup_interval
    if max_backups is not None:
        if max_backups < 1:
            _err("--max-backups must be at least 1.")
            sys.exit(1)
        updates["max_backups"] = max_backups
    if auto_update is not None:
        updates["auto_update"] = auto_update
    if update_interval is not None:
        if update_interval < 1:
            _err("--update-interval must be at least 1 hour.")
            sys.exit(1)
        updates["update_interval_hours"] = update_interval
    if port is not None:
        updates["port"] = port
        _patch_port(s, port)

    if not updates:
        _info(click.style(f"\n  Schedule config for '{name}':", bold=True))
        _info(f"  Auto-backup:     {'enabled' if s.auto_backup else 'disabled'}")
        _info(f"  Backup interval: every {s.backup_interval_hours}h")
        _info(f"  Max backups:     {s.max_backups}")
        _info(f"  Auto-update:     {'enabled' if s.auto_update else 'disabled'}")
        _info(f"  Update interval: every {s.update_interval_hours}h")
        _info(f"  Port:            {s.port}\n")
        return

    reg.update_server(name, updates)
    _ok(f"Updated config for '{name}':")
    for k, v in updates.items():
        _info(f"  {k} = {v}")


# ---------------------------------------------------------------------------
# Lifecycle commands
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("name")
def start(name: str) -> None:
    """Start a server."""
    _require(name)
    try:
        resp = ipc.request("start", server=name)
        _ok(f"Server '{name}' started (PID {resp['pid']}).")
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)


@cli.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Send SIGKILL instead of SIGTERM.")
def stop(name: str, force: bool) -> None:
    """Stop a server."""
    _require(name)
    try:
        ipc.request("stop", server=name, graceful=not force)
        _ok(f"Server '{name}' stopped.")
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)


@cli.command()
@click.argument("name")
def restart(name: str) -> None:
    """Restart a server."""
    _require(name)
    try:
        resp = ipc.request("restart", server=name)
        _ok(f"Server '{name}' restarted (PID {resp['pid']}).")
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)


@cli.command()
@click.argument("name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def status(name: str | None, as_json: bool) -> None:
    """Show server status. Omit NAME to show all servers."""
    reg = _registry()
    pm = _process_manager()

    servers = reg.list_servers() if name is None else [_require(name)]

    results = []
    for s in servers:
        st = pm.status(s)
        results.append({"name": s.name, **st})

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    for r in results:
        running = r["running"]
        label = click.style("RUNNING", fg="green") if running else click.style("STOPPED", fg="red")
        line = f"  {r['name']:<20} {label}"
        if running:
            if r["uptime_seconds"] is not None:
                line += f"  uptime: {_format_uptime(r['uptime_seconds'])}"
            if r["memory_mb"] is not None:
                line += f"  mem: {r['memory_mb']} MB"
        click.echo(line)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("name")
@click.option("--lines", "-n", default=config.LOG_TAIL_LINES, show_default=True, help="Lines to show.")
@click.option("--follow", "-f", is_flag=True, help="Follow log output in real time.")
@click.option("--search", "-s", default=None, help="Filter lines matching pattern.")
def logs(name: str, lines: int, follow: bool, search: str | None) -> None:
    """View server logs."""
    s = _require(name)
    lm = LogManager()
    if search:
        for line in lm.search(s, search):
            click.echo(line)
    elif follow:
        lm.follow(s)
    else:
        lm.tail(s, lines=lines)


# ---------------------------------------------------------------------------
# Run (send console command)
# ---------------------------------------------------------------------------

@cli.command("run")
@click.argument("name")
@click.argument("cmd")
def run_command(name: str, cmd: str) -> None:
    """Send a console command to a running server."""
    _require(name)
    try:
        ipc.request("run", server=name, cmd=cmd)
        _ok(f"Sent: {cmd}")
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("name")
@click.option("--version", "version_pin", default=None, help="Target version (default: latest).")
@click.option("--url", "direct_url", default=None, help="Direct download URL (skips Mojang page scrape).")
@click.option("--backup-first", is_flag=True, help="Create a backup before updating.")
def update(name: str, version_pin: str | None, direct_url: str | None, backup_first: bool) -> None:
    """Update a server to the latest (or specified) version."""
    reg = _registry()
    s = _require(name)
    pm = _process_manager()
    was_running = pm.is_running(s)

    if backup_first:
        _info("Creating pre-update backup...")
        try:
            bp = BackupManager().create(s, label="pre-update")
            _ok(f"Backup created: {bp.name}")
        except Exception as exc:
            _err(f"Backup failed: {exc}")
            sys.exit(1)

    if was_running:
        _info("Stopping server for update...")
        try:
            ipc.request("stop", server=name)
        except RuntimeError as exc:
            _err(str(exc))
            sys.exit(1)

    downloader = BedrockDownloader(config.CACHE_DIR)
    try:
        if direct_url:
            url = direct_url
            ver_match = re.search(r"bedrock-server-([\d.]+)\.zip", url)
            if not ver_match:
                _err("Could not extract version from URL.")
                sys.exit(1)
            version = ver_match.group(1)
        elif version_pin:
            version = version_pin
            url = f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{version}.zip"
        else:
            version, url = downloader.get_latest_version_url()

        if version == s.version and not (version_pin or direct_url):
            _ok(f"Server '{name}' is already at the latest version ({version}).")
            if was_running:
                try:
                    resp = ipc.request("start", server=name)
                    _ok(f"Server restarted (PID {resp['pid']}).")
                except RuntimeError as exc:
                    _err(str(exc))
                    sys.exit(1)
            return

        downloader.download(url, version, s.path, force=True)
    except Exception as exc:
        _err(str(exc))
        sys.exit(1)

    reg.update_server(name, {"version": version, "updated_at": datetime.now().isoformat()})
    _ok(f"Server '{name}' updated to {version}.")

    if was_running:
        _info("Restarting server...")
        try:
            resp = ipc.request("start", server=name)
            _ok(f"Server restarted (PID {resp['pid']}).")
        except RuntimeError as exc:
            _err(str(exc))
            sys.exit(1)


# ---------------------------------------------------------------------------
# Backup group
# ---------------------------------------------------------------------------

@cli.group()
def backup() -> None:
    """Manage server backups."""


@backup.command("create")
@click.argument("name")
@click.option("--label", default="", help="Optional label for the backup.")
def backup_create(name: str, label: str) -> None:
    """Create a backup of a server's world and config."""
    try:
        path = BackupManager().create(_require(name), label=label)
        _ok(f"Backup created: {path}")
    except Exception as exc:
        _err(str(exc))
        sys.exit(1)


@backup.command("list")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def backup_list(name: str, as_json: bool) -> None:
    """List backups for a server."""
    backups = BackupManager().list_backups(_require(name))
    if not backups:
        _info(f"No backups found for '{name}'.")
        return

    if as_json:
        click.echo(json.dumps(backups, indent=2))
        return

    header = f"  {'FILENAME':<50} {'SIZE':>8}  {'VERSION':<14}  {'LABEL'}"
    click.echo(click.style(header, bold=True))
    click.echo("  " + "-" * 90)
    for b in backups:
        click.echo(f"  {b['filename']:<50} {b['size_mb']:>6.1f}MB  {b['version']:<14}  {b['label']}")


@backup.command("restore")
@click.argument("name")
@click.argument("backup_file")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def backup_restore(name: str, backup_file: str, yes: bool) -> None:
    """Restore a server from a backup. Server must be stopped."""
    s = _require(name)
    pm = _process_manager()
    if pm.is_running(s):
        _err(f"Server '{name}' is running. Stop it before restoring.")
        sys.exit(1)

    bp = Path(backup_file)
    if not bp.is_absolute():
        bp = s.backup_dir / backup_file
    if not bp.exists():
        _err(f"Backup not found: {bp}")
        sys.exit(1)

    if not yes:
        click.confirm(f"Restore '{bp.name}' into '{name}'? This will overwrite current world data.", abort=True)

    try:
        BackupManager().restore(s, bp)
        _ok(f"Restored '{bp.name}' to server '{name}'.")
    except Exception as exc:
        _err(str(exc))
        sys.exit(1)


@backup.command("delete")
@click.argument("name")
@click.argument("backup_file")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def backup_delete(name: str, backup_file: str, yes: bool) -> None:
    """Delete a backup file."""
    s = _require(name)
    bp = Path(backup_file)
    if not bp.is_absolute():
        bp = s.backup_dir / backup_file

    if not yes:
        click.confirm(f"Delete backup '{bp.name}'?", abort=True)

    try:
        BackupManager().delete_backup(bp)
        _ok(f"Deleted: {bp.name}")
    except FileNotFoundError as exc:
        _err(str(exc))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Players group
# ---------------------------------------------------------------------------

@cli.group()
def players() -> None:
    """Manage players, whitelist, and permissions."""


@players.command("online")
@click.argument("name")
def players_online(name: str) -> None:
    """Show players currently online (parsed from server log)."""
    online = PlayerManager().get_online_players(_require(name))
    if not online:
        _info(f"No players currently online on '{name}'.")
        return
    _info(f"Online players on '{name}':")
    for p in online:
        click.echo(f"  - {p}")


@players.group("whitelist")
def whitelist() -> None:
    """Manage the server whitelist."""


@whitelist.command("add")
@click.argument("name")
@click.argument("player")
@click.option("--xuid", default="", help="Player XUID (optional but recommended).")
@click.option("--ignore-limit", is_flag=True, help="Allow player to bypass player limit.")
def whitelist_add(name: str, player: str, xuid: str, ignore_limit: bool) -> None:
    """Add PLAYER to the whitelist on NAME."""
    try:
        PlayerManager().whitelist_add(_require(name), player, xuid=xuid, ignores_player_limit=ignore_limit)
        _ok(f"Added '{player}' to whitelist on '{name}'.")
    except ValueError as exc:
        _err(str(exc))
        sys.exit(1)


@whitelist.command("remove")
@click.argument("name")
@click.argument("player")
def whitelist_remove(name: str, player: str) -> None:
    """Remove PLAYER from the whitelist on NAME."""
    try:
        PlayerManager().whitelist_remove(_require(name), player)
        _ok(f"Removed '{player}' from whitelist on '{name}'.")
    except ValueError as exc:
        _err(str(exc))
        sys.exit(1)


@whitelist.command("list")
@click.argument("name")
def whitelist_list(name: str) -> None:
    """List all whitelisted players on NAME."""
    entries = PlayerManager().whitelist_list(_require(name))
    if not entries:
        _info(f"Whitelist for '{name}' is empty.")
        return
    for e in entries:
        xuid = f"  xuid: {e['xuid']}" if e.get("xuid") else ""
        click.echo(f"  {e['name']}{xuid}")


@whitelist.command("enable")
@click.argument("name")
def whitelist_enable(name: str) -> None:
    """Enable the whitelist on NAME."""
    PlayerManager().whitelist_enable(_require(name))
    _ok(f"Whitelist enabled on '{name}'. Restart the server to apply.")


@whitelist.command("disable")
@click.argument("name")
def whitelist_disable(name: str) -> None:
    """Disable the whitelist on NAME."""
    PlayerManager().whitelist_disable(_require(name))
    _ok(f"Whitelist disabled on '{name}'. Restart the server to apply.")


@players.group("permissions")
def permissions() -> None:
    """Manage player permission levels."""


@permissions.command("set")
@click.argument("name")
@click.argument("player")
@click.option("--xuid", required=True, help="Player XUID.")
@click.option("--level", required=True, type=click.Choice(["visitor", "member", "operator"]), help="Permission level.")
def permissions_set(name: str, player: str, xuid: str, level: str) -> None:
    """Set PLAYER's permission level on NAME."""
    try:
        PlayerManager().permissions_set(_require(name), player, xuid, level)
        _ok(f"Set '{player}' ({xuid}) to '{level}' on '{name}'.")
    except ValueError as exc:
        _err(str(exc))
        sys.exit(1)


@permissions.command("list")
@click.argument("name")
def permissions_list(name: str) -> None:
    """List all player permissions on NAME."""
    entries = PlayerManager().permissions_list(_require(name))
    if not entries:
        _info(f"No permissions configured on '{name}'.")
        return
    for e in entries:
        click.echo(f"  {e.get('permission', '?'):<10}  xuid: {e.get('xuid', '?')}")


# ---------------------------------------------------------------------------
# Daemon group
# ---------------------------------------------------------------------------

@cli.group()
def daemon() -> None:
    """Manage the minectl background scheduler daemon."""


@daemon.command("start")
def daemon_start() -> None:
    """Start the scheduler daemon in the background."""
    from src.scheduler import get_daemon_pid

    if get_daemon_pid() is not None:
        _err("Daemon is already running.")
        sys.exit(1)

    pid = os.fork()
    if pid > 0:
        import time as _time
        _time.sleep(0.5)
        from src.scheduler import get_daemon_pid
        if get_daemon_pid():
            _ok(f"Daemon started (PID {pid}).")
        else:
            _err("Daemon may have failed to start. Check logs.")
        return

    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    setup_logging(config.MANAGER_LOG_FILE, level=config.LOG_LEVEL)
    from src.scheduler import DaemonScheduler
    DaemonScheduler().run()
    sys.exit(0)


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the scheduler daemon."""
    from src.scheduler import get_daemon_pid
    import signal as _signal

    pid = get_daemon_pid()
    if pid is None:
        _err("Daemon is not running.")
        sys.exit(1)

    os.kill(pid, _signal.SIGTERM)
    _ok(f"Sent SIGTERM to daemon (PID {pid}).")


@daemon.command("status")
def daemon_status() -> None:
    """Show scheduler daemon status and scheduled jobs."""
    from src.scheduler import get_daemon_pid

    pid = get_daemon_pid()
    if pid is None:
        _info(click.style("Daemon: STOPPED", fg="red"))
        return

    _info(click.style(f"Daemon: RUNNING (PID {pid})", fg="green"))

    servers = _registry().list_servers()
    scheduled = [s for s in servers if s.auto_backup or s.auto_update]
    if not scheduled:
        _info("  No servers have auto-backup or auto-update enabled.")
        _info("  Use 'minectl server configure NAME --auto-backup' to enable.")
        return

    _info(f"\n  {'SERVER':<20} {'BACKUP':<20} {'UPDATE'}")
    _info("  " + "-" * 60)
    for s in scheduled:
        backup_str = f"every {s.backup_interval_hours}h" if s.auto_backup else "off"
        update_str = f"every {s.update_interval_hours}h" if s.auto_update else "off"
        _info(f"  {s.name:<20} {backup_str:<20} {update_str}")


@daemon.command("run")
def daemon_run() -> None:
    """Run the scheduler in the foreground (for use with systemd)."""
    from src.scheduler import get_daemon_pid

    if get_daemon_pid() is not None:
        _err("Daemon is already running in the background.")
        sys.exit(1)

    setup_logging(config.MANAGER_LOG_FILE, level=config.LOG_LEVEL)
    _info("Starting minectl scheduler (foreground mode). Press Ctrl-C to stop.")
    from src.scheduler import DaemonScheduler
    DaemonScheduler().run()


@daemon.command("install")
@click.option("--enable", is_flag=True, default=True, show_default=True,
              help="Enable the unit so it starts on login.")
@click.option("--start", is_flag=True, default=False,
              help="Start the unit immediately after installing.")
def daemon_install(enable: bool, start: bool) -> None:
    """Install and enable a systemd user service for the scheduler daemon."""
    import shutil
    import subprocess as _sp

    minectl_bin = shutil.which("minectl") or str(Path(sys.argv[0]).resolve())

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / "minectl.service"

    unit_content = f"""\
[Unit]
Description=minectl Minecraft Bedrock server scheduler
After=network.target

[Service]
Type=simple
ExecStart={minectl_bin} daemon run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_content)
    _ok(f"Wrote service file: {unit_path}")

    result = _sp.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    if result.returncode != 0:
        _err(f"systemctl daemon-reload failed: {result.stderr.strip()}")
        sys.exit(1)
    _info("Reloaded systemd user daemon.")

    if enable:
        result = _sp.run(["systemctl", "--user", "enable", "minectl"], capture_output=True, text=True)
        if result.returncode != 0:
            _err(f"systemctl enable failed: {result.stderr.strip()}")
            sys.exit(1)
        _ok("Service enabled (will start automatically on login).")

    if start:
        result = _sp.run(["systemctl", "--user", "start", "minectl"], capture_output=True, text=True)
        if result.returncode != 0:
            _err(f"systemctl start failed: {result.stderr.strip()}")
            sys.exit(1)
        _ok("Service started.")
        _info("  Check status: systemctl --user status minectl")
        _info("  View logs:    journalctl --user -u minectl -f")
    else:
        _info(f"\nTo start now:  systemctl --user start minectl")
        _info(f"To view logs:  journalctl --user -u minectl -f")


@daemon.command("uninstall")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def daemon_uninstall(yes: bool) -> None:
    """Stop, disable, and remove the systemd user service."""
    import subprocess as _sp

    unit_path = Path.home() / ".config" / "systemd" / "user" / "minectl.service"
    if not unit_path.exists():
        _err("Service file not found — daemon may not be installed.")
        sys.exit(1)

    if not yes:
        click.confirm("Stop, disable, and remove the minectl systemd service?", abort=True)

    for cmd in [
        ["systemctl", "--user", "stop",    "minectl"],
        ["systemctl", "--user", "disable", "minectl"],
    ]:
        _sp.run(cmd, capture_output=True)

    unit_path.unlink()
    _sp.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    _ok("Service stopped, disabled, and removed.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_port(server: ServerInstance, port: int) -> None:
    """Set server-port (IPv4) and server-portv6 (IPv6 = port+1) in server.properties."""
    props = server.server_properties
    if not props.exists():
        return
    lines = props.read_text().splitlines()
    patched_v4 = patched_v6 = False
    for i, line in enumerate(lines):
        if line.startswith("server-port="):
            lines[i] = f"server-port={port}"
            patched_v4 = True
        elif line.startswith("server-portv6="):
            lines[i] = f"server-portv6={port + 1}"
            patched_v6 = True
    if not patched_v4:
        lines.append(f"server-port={port}")
    if not patched_v6:
        lines.append(f"server-portv6={port + 1}")
    props.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
