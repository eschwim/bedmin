"""minectl TUI — Textual-based interactive server manager."""

from __future__ import annotations

# Suppress tqdm output before importing modules that use it (tqdm respects this env var)
import os
os.environ["TQDM_DISABLE"] = "1"

# Suppress internal logging so it doesn't corrupt the terminal
import logging
logging.disable(logging.CRITICAL)

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from src.backup_manager import BackupManager
from src.downloader import BedrockDownloader
from src.player_manager import PlayerManager
from src import ipc
from src.process_manager import ProcessManager
from src.registry import ServerRegistry
from src.server_instance import ServerInstance

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

# ---------------------------------------------------------------------------
# Shared stateless manager singletons
# ---------------------------------------------------------------------------

_registry = ServerRegistry(config.REGISTRY_FILE)
_pm = ProcessManager()
_bm = BackupManager()
_plm = PlayerManager()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_uptime(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class PlayerInputModal(ModalScreen):
    """Modal dialog for entering player name and optional XUID."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def __init__(self, title: str, fields: list[str]) -> None:
        super().__init__()
        self._title = title
        self._fields = fields

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog"):
            yield Label(f"[bold]{self._title}[/]", id="modal-title")
            for i, field in enumerate(self._fields):
                yield Label(field)
                yield Input(placeholder=field, id=f"modal-input-{i}")
            with Horizontal(id="modal-buttons"):
                yield Button("OK",     id="modal-ok",     variant="primary")
                yield Button("Cancel", id="modal-cancel", variant="default")

    @on(Button.Pressed, "#modal-ok")
    def handle_ok(self) -> None:
        values = []
        for i in range(len(self._fields)):
            try:
                inp = self.query_one(f"#modal-input-{i}", Input)
                values.append(inp.value)
            except NoMatches:
                values.append("")
        self.dismiss(values)

    @on(Button.Pressed, "#modal-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen):
    """Simple yes/no confirmation dialog."""

    BINDINGS = [Binding("escape", "dismiss_no", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog"):
            yield Label(self._message, id="modal-title")
            with Horizontal(id="modal-buttons"):
                yield Button("Yes", id="modal-yes", variant="error")
                yield Button("No",  id="modal-no",  variant="default")

    @on(Button.Pressed, "#modal-yes")
    def handle_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#modal-no")
    def handle_no(self) -> None:
        self.dismiss(False)

    def action_dismiss_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Edit property value modal
# ---------------------------------------------------------------------------

class EditValueModal(ModalScreen):
    """Edit a single server.properties value."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def __init__(self, key: str, value: str) -> None:
        super().__init__()
        self._key = key
        self._value = value

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog"):
            yield Label(f"[bold]{self._key}[/]", id="modal-title")
            yield Input(value=self._value, id="edit-value")
            with Horizontal(id="modal-buttons"):
                yield Button("OK",     id="modal-ok",     variant="primary")
                yield Button("Cancel", id="modal-cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one("#edit-value", Input).focus()

    @on(Button.Pressed, "#modal-ok")
    def handle_ok(self) -> None:
        self.dismiss(self.query_one("#edit-value", Input).value)

    @on(Input.Submitted)
    def handle_submit(self) -> None:
        self.dismiss(self.query_one("#edit-value", Input).value)

    @on(Button.Pressed, "#modal-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Integer edit modal
# ---------------------------------------------------------------------------

class IntegerEditModal(ModalScreen):
    """Edit an integer property, restricting input to digits and validating range."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def __init__(self, key: str, value: str,
                 min_val: Optional[int], max_val: Optional[int]) -> None:
        super().__init__()
        self._key = key
        self._value = value
        self._min = min_val
        self._max = max_val

    def compose(self) -> ComposeResult:
        parts = []
        if self._min is not None and self._max is not None:
            parts.append(f"[{self._min}–{self._max}]")
        elif self._min is not None:
            parts.append(f"≥ {self._min}")
        elif self._max is not None:
            parts.append(f"≤ {self._max}")
        hint = f"  [dim]{parts[0]}[/]" if parts else ""
        with Container(id="modal-dialog"):
            yield Label(f"[bold]{self._key}[/]{hint}", id="modal-title")
            yield Input(value=self._value, restrict=r"[0-9]*", id="edit-value")
            yield Label("", id="edit-error")
            with Horizontal(id="modal-buttons"):
                yield Button("OK",     id="modal-ok",     variant="primary")
                yield Button("Cancel", id="modal-cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one("#edit-value", Input).focus()

    def _validate(self, text: str) -> bool:
        if not text:
            return False
        try:
            v = int(text)
        except ValueError:
            return False
        if self._min is not None and v < self._min:
            return False
        if self._max is not None and v > self._max:
            return False
        return True

    @on(Button.Pressed, "#modal-ok")
    def handle_ok(self) -> None:
        val = self.query_one("#edit-value", Input).value.strip()
        if self._validate(val):
            self.dismiss(val)
        else:
            hint = ""
            if self._min is not None and self._max is not None:
                hint = f" (must be {self._min}–{self._max})"
            elif self._min is not None:
                hint = f" (must be ≥ {self._min})"
            self.query_one("#edit-error", Label).update(
                f"[red]Invalid integer{hint}[/]"
            )

    @on(Input.Submitted)
    def handle_submit(self) -> None:
        self.handle_ok()

    @on(Button.Pressed, "#modal-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Select value modal (for properties with a finite set of allowed values)
# ---------------------------------------------------------------------------

class SelectValueModal(ModalScreen):
    """Pick one value from a list of allowed options."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def __init__(self, key: str, current: str, options: list[str]) -> None:
        super().__init__()
        self._key = key
        self._current = current
        self._options = options

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog"):
            yield Label(f"[bold]{self._key}[/]", id="modal-title")
            yield ListView(id="select-list")
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="modal-cancel", variant="default")

    def on_mount(self) -> None:
        lv = self.query_one("#select-list", ListView)
        for opt in self._options:
            marker = "● " if opt == self._current else "  "
            lv.append(ListItem(Label(f"{marker}{opt}")))
        if self._current in self._options:
            lv.index = self._options.index(self._current)
        lv.focus()

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._options):
            self.dismiss(self._options[idx])

    @on(Button.Pressed, "#modal-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Create server modal
# ---------------------------------------------------------------------------

class CreateServerModal(ModalScreen):
    """Dialog for creating a new server instance."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog"):
            yield Label("[bold]Create New Server[/]", id="modal-title")
            yield Label("Server name")
            yield Input(placeholder="e.g. survival", id="create-name")
            yield Label(f"Port (default: {config.DEFAULT_PORT})")
            yield Input(placeholder=str(config.DEFAULT_PORT), id="create-port")
            with Horizontal(id="modal-buttons"):
                yield Button("Create", id="modal-ok",     variant="primary")
                yield Button("Cancel", id="modal-cancel", variant="default")

    @on(Button.Pressed, "#modal-ok")
    def handle_ok(self) -> None:
        name = self.query_one("#create-name", Input).value.strip()
        port_str = self.query_one("#create-port", Input).value.strip()
        if not name:
            self.query_one("#create-name", Input).focus()
            return
        try:
            port = int(port_str) if port_str else config.DEFAULT_PORT
        except ValueError:
            self.query_one("#create-port", Input).focus()
            return
        self.dismiss((name, port))

    @on(Button.Pressed, "#modal-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Sidebar widgets
# ---------------------------------------------------------------------------

class ServerListItem(ListItem):
    """One server row in the sidebar list."""

    def __init__(self, server: ServerInstance, running: bool) -> None:
        super().__init__()
        self.server = server
        self._running = running

    def compose(self) -> ComposeResult:
        indicator = "●" if self._running else "○"
        yield Label(f"{indicator} {self.server.name}", classes="server-item-label")

    def refresh_indicator(self, running: bool) -> None:
        self._running = running
        self.set_class(running, "running")
        self.set_class(not running, "stopped")
        try:
            indicator = "●" if running else "○"
            self.query_one(".server-item-label", Label).update(
                f"{indicator} {self.server.name}"
            )
        except NoMatches:
            pass


# ---------------------------------------------------------------------------
# Tab widgets
# ---------------------------------------------------------------------------

class StatusCard(Static):
    """Status block: PID, uptime, memory, version, port."""

    def update_status(self, server: ServerInstance, status: dict) -> None:
        running = status.get("running", False)
        self.set_class(running, "running")
        self.set_class(not running, "stopped")
        if running:
            uptime_s = status.get("uptime_seconds")
            uptime_str = _format_uptime(uptime_s) if uptime_s is not None else "N/A"
            mem = status.get("memory_mb")
            mem_str = f"{mem} MB" if mem is not None else "N/A"
            content = (
                f"[bold green]● RUNNING[/]   PID: {status['pid']}\n"
                f"Uptime:  {uptime_str}    Memory: {mem_str}\n"
                f"Version: {server.version}    Port: {server.port}"
            )
        else:
            content = (
                f"[bold red]○ STOPPED[/]\n"
                f"Version: {server.version}    Port: {server.port}"
            )
        self.update(content)


class OverviewTab(Widget):
    """Overview: status card + action buttons."""

    def __init__(self, server: ServerInstance) -> None:
        super().__init__()
        self.server = server

    def compose(self) -> ComposeResult:
        yield StatusCard(id="status-card")
        with Horizontal(id="action-bar"):
            yield Button("Start",   id="btn-start",   variant="success")
            yield Button("Stop",    id="btn-stop",    variant="error")
            yield Button("Restart", id="btn-restart", variant="warning")
            yield Button("Update",  id="btn-update",  variant="primary")
            yield Button("Backup",  id="btn-backup",  variant="default")

    def on_mount(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        status = _pm.status(self.server)
        self.query_one(StatusCard).update_status(self.server, status)
        running = status["running"]
        self.query_one("#btn-start",   Button).disabled = running
        self.query_one("#btn-stop",    Button).disabled = not running
        self.query_one("#btn-restart", Button).disabled = not running


class LogsTab(Widget):
    """Live log tail using RichLog."""

    def __init__(self, server: ServerInstance) -> None:
        super().__init__()
        self.server = server
        self._tail_proc: Optional[subprocess.Popen] = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="log-view", highlight=False, markup=True, wrap=True)

    def on_mount(self) -> None:
        self._seed_history()
        self._start_tail()

    def on_unmount(self) -> None:
        if self._tail_proc:
            try:
                self._tail_proc.terminate()
            except OSError:
                pass

    def _seed_history(self) -> None:
        if not self.server.log_file.exists():
            return
        result = subprocess.run(
            ["tail", "-n", "200", str(self.server.log_file)],
            capture_output=True, text=True,
        )
        log = self.query_one(RichLog)
        for line in result.stdout.splitlines():
            log.write(self._markup(line))

    @work(thread=True)
    def _start_tail(self) -> None:
        if not self.server.log_file.exists():
            return
        proc = subprocess.Popen(
            ["tail", "-f", "-n", "0", str(self.server.log_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._tail_proc = proc
        try:
            for raw_line in proc.stdout:
                if not self.is_attached:
                    break
                markup = self._markup(raw_line.rstrip())
                self.app.call_from_thread(self.query_one(RichLog).write, markup)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.terminate()
            except OSError:
                pass

    @staticmethod
    def _markup(line: str) -> str:
        import re
        from rich.markup import escape
        safe = escape(line)
        if re.search(r"\[ERROR\]", line, re.IGNORECASE):
            return f"[red]{safe}[/]"
        if re.search(r"\[WARN", line, re.IGNORECASE):
            return f"[yellow]{safe}[/]"
        if re.search(r"Player (connected|disconnected):", line, re.IGNORECASE):
            return f"[cyan]{safe}[/]"
        return safe


class PlayersTab(Widget):
    """Online player list + whitelist management."""

    def __init__(self, server: ServerInstance) -> None:
        super().__init__()
        self.server = server

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="online-section"):
            yield Label("[bold]Online Players[/]")
            yield Label("", id="online-list")
        yield DataTable(id="whitelist-table", cursor_type="row")
        with Horizontal(id="player-action-bar"):
            yield Button("Add to Whitelist",      id="btn-wl-add",    variant="success")
            yield Button("Remove from Whitelist",  id="btn-wl-remove", variant="error")
            yield Button("Refresh",               id="btn-players-refresh", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#whitelist-table", DataTable)
        table.add_columns("Name", "XUID", "Bypass Limit")
        self.refresh_data()

    def refresh_data(self) -> None:
        online = _plm.get_online_players(self.server)
        text = ", ".join(online) if online else "[dim]None[/]"
        try:
            self.query_one("#online-list", Label).update(text)
        except NoMatches:
            pass

        table = self.query_one("#whitelist-table", DataTable)
        table.clear()
        for entry in _plm.whitelist_list(self.server):
            bypass = "Yes" if entry.get("ignoresPlayerLimit") else "No"
            table.add_row(entry.get("name", ""), entry.get("xuid", ""), bypass)


class BackupsTab(Widget):
    """Backup table with create/restore/delete."""

    def __init__(self, server: ServerInstance) -> None:
        super().__init__()
        self.server = server
        self._backup_paths: list[str] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="backup-table", cursor_type="row")
        with Horizontal(id="backup-action-bar"):
            yield Button("Create",  id="btn-backup-create",  variant="success")
            yield Button("Restore", id="btn-backup-restore", variant="warning")
            yield Button("Delete",  id="btn-backup-delete",  variant="error")
            yield Button("Refresh", id="btn-backup-refresh", variant="default")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Filename", "Size", "Version", "Label", "Created")
        self.refresh_data()

    def refresh_data(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._backup_paths = []
        for b in _bm.list_backups(self.server):
            created = b["created_at"][:19].replace("T", " ") if b["created_at"] else ""
            table.add_row(
                b["filename"],
                f"{b['size_mb']} MB",
                b["version"],
                b["label"],
                created,
            )
            self._backup_paths.append(b["path"])

    def get_selected_backup_path(self) -> Optional[str]:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self._backup_paths):
            return None
        return self._backup_paths[row]


# ---------------------------------------------------------------------------
# Properties tab
# ---------------------------------------------------------------------------

class PropertiesTab(Widget):
    """View and edit server.properties key-value pairs."""

    def __init__(self, server: ServerInstance) -> None:
        super().__init__()
        self.server = server
        # Each entry: (key, value, constraints)
        # constraints dict:
        #   {'options': [...]}               → SelectValueModal
        #   {'integer': True, 'min': x, 'max': y}  → IntegerEditModal
        #   {}                               → EditValueModal (free text)
        self._props: list[tuple[str, str, dict]] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="props-table", cursor_type="row")
        with Horizontal(id="props-action-bar"):
            yield Button("Save",   id="btn-props-save",   variant="success")
            yield Button("Reload", id="btn-props-reload", variant="default")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Property", "Value")
        self.load_props()

    def load_props(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._props = []
        if not self.server.server_properties.exists():
            return

        lines = self.server.server_properties.read_text().splitlines()
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped.startswith("#") and "=" in stripped:
                key, _, value = stripped.partition("=")
                key, value = key.strip(), value.strip()
                # Collect the comment block that immediately follows (stop at blank line)
                j = i + 1
                comment_text = []
                while j < len(lines):
                    s = lines[j].strip()
                    if s.startswith("#"):
                        comment_text.append(s[1:].strip())
                        j += 1
                    else:
                        break
                constraints = self._build_constraints(value, comment_text)
                self._props.append((key, value, constraints))
                table.add_row(key, value)
            i += 1

    @staticmethod
    def _build_constraints(value: str, comment_lines: list[str]) -> dict:
        """Return a constraints dict for the appropriate edit modal."""
        for line in comment_lines:
            if not line.lower().startswith("allowed values:"):
                continue
            rest = line[len("allowed values:"):].strip()
            rest_lower = rest.lower().rstrip(".")

            # Finite set of quoted options → selection
            quoted = re.findall(r'"([^"]+)"', rest)
            if len(quoted) >= 2:
                return {"options": quoted}

            # Unquoted true/false → boolean selection
            if re.fullmatch(
                r'true[,\s]+false|false[,\s]+true|true or false|false or true',
                rest_lower, re.IGNORECASE,
            ):
                return {"options": ["true", "false"]}

            # Integer range: [min, max]
            m = re.search(r'\[(\d+),\s*(\d+)\]', rest)
            if m:
                return {"integer": True, "min": int(m.group(1)), "max": int(m.group(2))}

            # Integer with lower bound only
            if re.search(r'positive integer equal to (\d+) or greater', rest_lower):
                n = int(re.search(r'(\d+)', rest_lower).group(1))
                return {"integer": True, "min": n, "max": None}
            if "positive integer" in rest_lower or "any positive integer" in rest_lower:
                return {"integer": True, "min": 1, "max": None}
            if "non-negative integer" in rest_lower:
                return {"integer": True, "min": 0, "max": None}
            if re.search(r'\bany integer\b|\binteger\b', rest_lower):
                return {"integer": True, "min": None, "max": None}

        # No "Allowed values:" found — infer from current value
        if value.lower() in ("true", "false"):
            return {"options": ["true", "false"]}
        return {}

    def save_props(self) -> None:
        if not self.server.server_properties.exists():
            return
        values = {k: v for k, v, _c in self._props}
        lines = self.server.server_properties.read_text().splitlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in values:
                    new_lines.append(f"{key}={values[key]}")
                    continue
            new_lines.append(line)
        self.server.server_properties.write_text("\n".join(new_lines) + "\n")

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row < 0 or row >= len(self._props):
            return
        key, value, constraints = self._props[row]
        if "options" in constraints:
            screen = SelectValueModal(key, value, constraints["options"])
        elif "integer" in constraints:
            screen = IntegerEditModal(
                key, value, constraints.get("min"), constraints.get("max")
            )
        else:
            screen = EditValueModal(key, value)
        self.app.push_screen(screen, callback=lambda v: self._apply_edit(row, v))

    def _apply_edit(self, row: int, new_value: Optional[str]) -> None:
        if new_value is None:
            return
        key, _, constraints = self._props[row]
        self._props[row] = (key, new_value, constraints)
        try:
            self.query_one("#props-table", DataTable).update_cell_at((row, 1), new_value)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server panel (right pane)
# ---------------------------------------------------------------------------

class ServerPanel(Widget):
    """Right pane: tabbed content for the currently selected server."""

    server: reactive[Optional[ServerInstance]] = reactive(None, recompose=True)

    def compose(self) -> ComposeResult:
        if self.server is None:
            yield Static(
                "[dim]Select a server from the sidebar\nor press [bold]r[/] to refresh.[/]",
                id="empty-state",
            )
            return

        s = self.server
        with TabbedContent(id="server-tabs"):
            with TabPane("Overview", id="tab-overview"):
                yield OverviewTab(s)
            with TabPane("Logs", id="tab-logs"):
                yield LogsTab(s)
            with TabPane("Players", id="tab-players"):
                yield PlayersTab(s)
            with TabPane("Backups", id="tab-backups"):
                yield BackupsTab(s)
            with TabPane("Properties", id="tab-properties"):
                yield PropertiesTab(s)

    def poll_status(self) -> None:
        """Called by the app-level timer every 2 seconds."""
        try:
            self.query_one(OverviewTab).refresh_status()
        except NoMatches:
            pass

    # --- Button handlers (events bubble up from child tabs) ---

    @on(Button.Pressed, "#btn-start")
    def handle_start(self) -> None:
        if self.server:
            self._do_start(self.server)

    @on(Button.Pressed, "#btn-stop")
    def handle_stop(self) -> None:
        if self.server:
            self._do_stop(self.server)

    @on(Button.Pressed, "#btn-restart")
    def handle_restart(self) -> None:
        if self.server:
            self._do_restart(self.server)

    @on(Button.Pressed, "#btn-update")
    def handle_update(self) -> None:
        if self.server:
            self._do_update(self.server)

    @on(Button.Pressed, "#btn-backup")
    def handle_quick_backup(self) -> None:
        if self.server:
            self._do_backup(self.server, label="manual")

    @on(Button.Pressed, "#btn-backup-create")
    def handle_backup_create(self) -> None:
        if self.server:
            self._do_backup(self.server, label="")

    @on(Button.Pressed, "#btn-backup-restore")
    def handle_backup_restore(self) -> None:
        if self.server is None:
            return
        try:
            path = self.query_one(BackupsTab).get_selected_backup_path()
        except NoMatches:
            return
        if path:
            self.app.push_screen(
                ConfirmModal(f"Restore [bold]{Path(path).name}[/]?\nThis will overwrite current world data."),
                callback=lambda ok: self._do_restore(self.server, Path(path)) if ok else None,
            )

    @on(Button.Pressed, "#btn-backup-delete")
    def handle_backup_delete(self) -> None:
        if self.server is None:
            return
        try:
            path = self.query_one(BackupsTab).get_selected_backup_path()
        except NoMatches:
            return
        if path:
            self.app.push_screen(
                ConfirmModal(f"Delete [bold]{Path(path).name}[/]?"),
                callback=lambda ok: self._do_delete_backup(self.server, Path(path)) if ok else None,
            )

    @on(Button.Pressed, "#btn-backup-refresh")
    def handle_backup_refresh(self) -> None:
        try:
            self.query_one(BackupsTab).refresh_data()
        except NoMatches:
            pass

    @on(Button.Pressed, "#btn-wl-add")
    def handle_wl_add(self) -> None:
        if self.server is None:
            return
        self.app.push_screen(
            PlayerInputModal("Add to Whitelist", ["Player name", "XUID (optional)"]),
            callback=self._on_wl_add_result,
        )

    @on(Button.Pressed, "#btn-wl-remove")
    def handle_wl_remove(self) -> None:
        if self.server is None:
            return
        try:
            table = self.query_one("#whitelist-table", DataTable)
            row = table.cursor_row
            # Get name from first column of selected row
            name = str(table.get_cell_at((row, 0))) if row >= 0 else ""
        except (NoMatches, Exception):
            name = ""
        self.app.push_screen(
            PlayerInputModal("Remove from Whitelist", ["Player name"]),
            callback=self._on_wl_remove_result,
        )

    @on(Button.Pressed, "#btn-players-refresh")
    def handle_players_refresh(self) -> None:
        try:
            self.query_one(PlayersTab).refresh_data()
        except NoMatches:
            pass

    @on(Button.Pressed, "#btn-props-save")
    def handle_props_save(self) -> None:
        try:
            self.query_one(PropertiesTab).save_props()
            self.app.notify("server.properties saved.", title="Saved")
        except NoMatches:
            pass

    @on(Button.Pressed, "#btn-props-reload")
    def handle_props_reload(self) -> None:
        try:
            self.query_one(PropertiesTab).load_props()
        except NoMatches:
            pass

    def _on_wl_add_result(self, result: Optional[list[str]]) -> None:
        if result is None or self.server is None:
            return
        name = result[0].strip() if result else ""
        xuid = result[1].strip() if len(result) > 1 else ""
        if name:
            self._do_wl_add(self.server, name, xuid)

    def _on_wl_remove_result(self, result: Optional[list[str]]) -> None:
        if result is None or self.server is None:
            return
        name = result[0].strip() if result else ""
        if name:
            self._do_wl_remove(self.server, name)

    # --- Workers (all blocking operations) ---

    @work(thread=True, exclusive=True, group="server-ops")
    def _do_start(self, server: ServerInstance) -> None:
        try:
            resp = ipc.request("start", server=server.name)
            self.app.call_from_thread(
                self.app.notify, f"Started '{server.name}' (PID {resp['pid']})", title="Server Started"
            )
        except RuntimeError as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Start Failed", severity="error"
            )
        finally:
            self.app.call_from_thread(self.poll_status)

    @work(thread=True, exclusive=True, group="server-ops")
    def _do_stop(self, server: ServerInstance) -> None:
        try:
            ipc.request("stop", server=server.name)
            self.app.call_from_thread(
                self.app.notify, f"Stopped '{server.name}'", title="Server Stopped"
            )
        except RuntimeError as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Stop Failed", severity="error"
            )
        finally:
            self.app.call_from_thread(self.poll_status)

    @work(thread=True, exclusive=True, group="server-ops")
    def _do_restart(self, server: ServerInstance) -> None:
        try:
            resp = ipc.request("restart", server=server.name)
            self.app.call_from_thread(
                self.app.notify, f"Restarted '{server.name}' (PID {resp['pid']})", title="Restarted"
            )
        except RuntimeError as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Restart Failed", severity="error"
            )
        finally:
            self.app.call_from_thread(self.poll_status)

    @work(thread=True, exclusive=True, group="server-ops")
    def _do_update(self, server: ServerInstance) -> None:
        try:
            downloader = BedrockDownloader(config.CACHE_DIR)
            version, url = downloader.get_latest_version_url()
            was_running = _pm.is_running(server)
            if was_running:
                ipc.request("stop", server=server.name)
            downloader.download(url, version, server.path, force=True)
            _registry.update_server(server.name, {"version": version})
            if was_running:
                ipc.request("start", server=server.name)
            self.app.call_from_thread(
                self.app.notify, f"Updated to {version}", title="Update Complete"
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Update Failed", severity="error"
            )
        finally:
            self.app.call_from_thread(self.poll_status)

    @work(thread=True, group="backup-ops")
    def _do_backup(self, server: ServerInstance, label: str) -> None:
        try:
            path = _bm.create(server, label=label)
            self.app.call_from_thread(
                self.app.notify, f"Created: {path.name}", title="Backup"
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Backup Failed", severity="error"
            )
        finally:
            try:
                self.app.call_from_thread(self.query_one(BackupsTab).refresh_data)
            except NoMatches:
                pass

    @work(thread=True, group="backup-ops")
    def _do_restore(self, server: ServerInstance, backup_path: Path) -> None:
        if _pm.is_running(server):
            self.app.call_from_thread(
                self.app.notify,
                "Stop the server before restoring a backup.",
                title="Cannot Restore",
                severity="warning",
            )
            return
        try:
            _bm.restore(server, backup_path)
            self.app.call_from_thread(
                self.app.notify, f"Restored {backup_path.name}", title="Restore Complete"
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Restore Failed", severity="error"
            )

    @work(thread=True, group="backup-ops")
    def _do_delete_backup(self, server: ServerInstance, backup_path: Path) -> None:
        try:
            _bm.delete_backup(backup_path)
            self.app.call_from_thread(
                self.app.notify, f"Deleted {backup_path.name}", title="Backup Deleted"
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Delete Failed", severity="error"
            )
        finally:
            try:
                self.app.call_from_thread(self.query_one(BackupsTab).refresh_data)
            except NoMatches:
                pass

    @work(thread=True)
    def _do_wl_add(self, server: ServerInstance, name: str, xuid: str) -> None:
        try:
            _plm.whitelist_add(server, name, xuid=xuid)
            self.app.call_from_thread(
                self.app.notify, f"Added '{name}' to whitelist", title="Whitelist"
            )
        except ValueError as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Whitelist Error", severity="error"
            )
        finally:
            try:
                self.app.call_from_thread(self.query_one(PlayersTab).refresh_data)
            except NoMatches:
                pass

    @work(thread=True)
    def _do_wl_remove(self, server: ServerInstance, name: str) -> None:
        try:
            _plm.whitelist_remove(server, name)
            self.app.call_from_thread(
                self.app.notify, f"Removed '{name}' from whitelist", title="Whitelist"
            )
        except ValueError as exc:
            self.app.call_from_thread(
                self.app.notify, str(exc), title="Whitelist Error", severity="error"
            )
        finally:
            try:
                self.app.call_from_thread(self.query_one(PlayersTab).refresh_data)
            except NoMatches:
                pass


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class MinectlApp(App):
    """minectl TUI — Minecraft Bedrock server manager."""

    TITLE = "minectl"
    SUB_TITLE = "Minecraft Bedrock Server Manager"

    CSS = """
/* ── Layout ─────────────────────────────────────────────────── */
#layout {
    height: 1fr;
}

#sidebar {
    width: 24;
    border-right: solid $primary-darken-2;
    background: $surface;
}

#sidebar-title {
    text-align: center;
    background: $primary-darken-2;
    color: $text;
    padding: 0 1;
    text-style: bold;
}

#server-list {
    height: 1fr;
}

#main-panel {
    width: 1fr;
    height: 1fr;
}

/* ── Button focus / hover ────────────────────────────────────── */
Button:focus, Button.-style-default:focus {
    background-tint: $foreground 35%;
}

Button.-primary:hover  { background: $primary-lighten-1; }
Button.-success:hover  { background: $success-lighten-1; }
Button.-warning:hover  { background: $warning-lighten-1; }
Button.-error:hover    { background: $error-lighten-1;   }

/* ── Default-variant buttons ─────────────────────────────────── */
Button.-style-default {
    background: $primary-darken-2;
    color: $text;
    border-top: tall $primary-darken-1;
    border-bottom: tall $primary-darken-3;
}

Button.-style-default:hover {
    background: $primary-lighten-1;
}

/* ── Tab header colors ───────────────────────────────────────── */
Tab {
    color: $text-muted;
}

Tab.-active {
    color: $text;
}

/* ── Ensure tab content fills available space ────────────────── */
OverviewTab, LogsTab, PlayersTab, BackupsTab, PropertiesTab {
    height: 1fr;
}

/* ── Status card ─────────────────────────────────────────────── */
#status-card {
    border: round $primary;
    padding: 1 2;
    margin: 1 1 0 1;
    height: auto;
}

#status-card.running {
    border: round $success;
}

#status-card.stopped {
    border: round $error;
}

/* ── Action buttons ──────────────────────────────────────────── */
#action-bar {
    height: 3;
    margin: 1;
}

#action-bar Button {
    margin-right: 1;
}

/* ── Logs ────────────────────────────────────────────────────── */
#log-view {
    height: 1fr;
    margin: 1;
    border: solid $surface-lighten-2;
}

/* ── Players ─────────────────────────────────────────────────── */
#online-section {
    height: auto;
    max-height: 6;
    border: round $primary;
    margin: 1 1 0 1;
    padding: 0 1;
}

#whitelist-table {
    height: 1fr;
    margin: 1 1 0 1;
}

#player-action-bar {
    height: 3;
    margin: 0 1 1 1;
}

#player-action-bar Button {
    margin-right: 1;
}

/* ── Backups ─────────────────────────────────────────────────── */
#backup-table {
    height: 1fr;
    margin: 1 1 0 1;
}

#backup-action-bar {
    height: 3;
    margin: 0 1 1 1;
}

#backup-action-bar Button {
    margin-right: 1;
}

/* ── Properties ─────────────────────────────────────────────── */
#props-table {
    height: 1fr;
    margin: 1 1 0 1;
}

#props-action-bar {
    height: 3;
    margin: 0 1 1 1;
}

#props-action-bar Button {
    margin-right: 1;
}

/* ── Command bar ─────────────────────────────────────────────── */
#command-bar {
    height: 3;
    border-top: solid $primary-darken-2;
    padding: 0 1;
    background: $surface;
}

#cmd-prompt {
    width: auto;
    padding: 1 1 0 0;
    color: $primary;
}

#cmd-input {
    width: 1fr;
}

/* ── Empty state ─────────────────────────────────────────────── */
#empty-state {
    align: center middle;
    width: 1fr;
    height: 1fr;
    color: $text-muted;
    text-align: center;
}

/* ── Sidebar items ───────────────────────────────────────────── */
ServerListItem {
    height: 1;
    padding: 0 1;
}

ServerListItem.running .server-item-label {
    color: $success;
}

ServerListItem.stopped .server-item-label {
    color: $text-muted;
}

ServerListItem.-highlight .server-item-label {
    color: $text;
}

/* ── Modals ──────────────────────────────────────────────────── */
PlayerInputModal, ConfirmModal {
    align: center middle;
}

#modal-dialog {
    width: 60;
    height: auto;
    border: thick $primary;
    background: $surface;
    padding: 1 2;
}

#modal-title {
    margin-bottom: 1;
    text-style: bold;
}

#modal-buttons {
    margin-top: 1;
    height: 3;
}

#modal-buttons Button {
    margin-right: 1;
}
"""

    BINDINGS = [
        Binding("q",      "quit",         "Quit"),
        Binding("r",      "refresh",      "Refresh"),
        Binding("n",      "new_server",   "New Server"),
        Binding("s",      "start_server", "Start",   show=False),
        Binding("t",      "stop_server",  "Stop",    show=False),
        Binding("ctrl+b", "quick_backup", "Backup",  show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with Vertical(id="sidebar"):
                yield Static("SERVERS", id="sidebar-title")
                yield ListView(id="server-list")
            yield ServerPanel(id="main-panel")
        with Horizontal(id="command-bar"):
            yield Label(">", id="cmd-prompt")
            yield Input(
                placeholder="Send console command to selected server (Enter to send)...",
                id="cmd-input",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._load_servers()
        self.set_interval(2, self._poll_all)

    # --- Server list ---

    def _load_servers(self) -> None:
        lv = self.query_one("#server-list", ListView)
        lv.clear()
        servers = _registry.list_servers()
        for server in servers:
            running = _pm.is_running(server)
            item = ServerListItem(server, running)
            lv.append(item)
        # Auto-select first server if any
        if servers:
            lv.index = 0
            self._select_server(servers[0])

    def _select_server(self, server: ServerInstance) -> None:
        self.query_one(ServerPanel).server = server

    # --- Polling ---

    def _poll_all(self) -> None:
        lv = self.query_one("#server-list", ListView)
        for item in lv.query(ServerListItem):
            item.refresh_indicator(_pm.is_running(item.server))
        self.query_one(ServerPanel).poll_status()

    # --- Events ---

    @on(ListView.Selected)
    def on_server_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ServerListItem):
            self._select_server(event.item.server)

    @on(Input.Submitted, "#cmd-input")
    def on_command_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        if not cmd:
            return
        event.input.value = ""
        server = self.query_one(ServerPanel).server
        if server is None:
            self.notify("No server selected.", severity="warning")
            return
        self._do_send_command(server, cmd)

    @work(thread=True)
    def _do_send_command(self, server: ServerInstance, cmd: str) -> None:
        try:
            ipc.request("run", server=server.name, cmd=cmd)
            self.app.call_from_thread(self.notify, f"Sent: {cmd}", title="Command Sent")
        except RuntimeError as exc:
            self.app.call_from_thread(
                self.notify, str(exc), title="Command Error", severity="error"
            )

    # --- Key actions ---

    def action_refresh(self) -> None:
        self._load_servers()
        self.notify("Server list refreshed.", timeout=2)

    def action_new_server(self) -> None:
        self.push_screen(CreateServerModal(), callback=self._on_create_result)

    def _on_create_result(self, result: Optional[tuple]) -> None:
        if result:
            name, port = result
            self._do_create_server(name, port)

    @work(thread=True)
    def _do_create_server(self, name: str, port: int) -> None:
        if _registry.get_server(name):
            self.app.call_from_thread(
                self.notify, f"Server '{name}' already exists.", severity="error"
            )
            return

        dest = config.DEFAULT_SERVERS_DIR / name
        downloader = BedrockDownloader(config.CACHE_DIR)

        self.app.call_from_thread(
            self.notify, f"Downloading server for '{name}'…", title="Creating", timeout=120
        )
        try:
            version, url = downloader.get_latest_version_url()
            downloader.download(url, version, dest)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, str(exc), title="Create Failed", severity="error"
            )
            return

        now = datetime.now().isoformat()
        instance = ServerInstance(
            name=name, path=dest, version=version,
            port=port, created_at=now, updated_at=now,
        )
        # Write IPv4 and IPv6 ports into server.properties
        props = instance.server_properties
        if props.exists():
            lines = props.read_text().splitlines()
            patched_v4 = patched_v6 = False
            for idx, line in enumerate(lines):
                if line.startswith("server-port="):
                    lines[idx] = f"server-port={port}"
                    patched_v4 = True
                elif line.startswith("server-portv6="):
                    lines[idx] = f"server-portv6={port + 1}"
                    patched_v6 = True
            if not patched_v4:
                lines.append(f"server-port={port}")
            if not patched_v6:
                lines.append(f"server-portv6={port + 1}")
            props.write_text("\n".join(lines) + "\n")

        try:
            _registry.add_server(instance)
        except ValueError as exc:
            self.app.call_from_thread(
                self.notify, str(exc), title="Create Failed", severity="error"
            )
            return

        self.app.call_from_thread(
            self.notify, f"Server '{name}' created (v{version})", title="Done"
        )
        self.app.call_from_thread(self._load_servers)

    def action_start_server(self) -> None:
        panel = self.query_one(ServerPanel)
        if panel.server:
            panel._do_start(panel.server)

    def action_stop_server(self) -> None:
        panel = self.query_one(ServerPanel)
        if panel.server:
            panel._do_stop(panel.server)

    def action_quick_backup(self) -> None:
        panel = self.query_one(ServerPanel)
        if panel.server:
            panel._do_backup(panel.server, label="manual")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    MinectlApp().run()


if __name__ == "__main__":
    main()
