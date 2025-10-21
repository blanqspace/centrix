"""Textual-based control interface for Centrix."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, Log, Static

from centrix.core.logging import ensure_runtime_dirs, log_event, warn_on_local_env
from centrix.core.metrics import snapshot_kpis
from centrix.ipc import is_running, pidfile, read_state
from centrix.ipc.bus import Bus
from centrix.settings import get_settings

SERVICE_NAMES: list[str] = ["tui", "dashboard", "worker", "slack"]


class ControlApp(App[None]):
    """Centrix control panel skeleton."""

    CSS = (
        "#status { padding: 1 2; } "
        "#events { height: 1fr; padding: 1 2; } "
        "#command { padding: 0 2; }"
    )

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+r", "run", "Run", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        Binding("ctrl+m", "mode", "Mode", priority=True),
        Binding("ctrl+p", "pause", "Pause", priority=True),
        Binding("ctrl+o", "order", "Order", priority=True),
        Binding("ctrl+k", "palette", "Command", priority=True),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        ensure_runtime_dirs()
        warn_on_local_env("tui")
        self._settings = get_settings()
        self._python_bin = Path(".venv/bin/python")
        if not self._python_bin.exists():
            self._python_bin = Path(sys.executable)
        self._bus = Bus(self._settings.ipc_db)
        self._status: Static | None = None
        self._events: Log | None = None
        self._command: Input | None = None
        self._last_event_id = 0
        self._service_total = len(SERVICE_NAMES)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._status = Static("Centrix ready", id="status")
        yield self._status
        self._events = Log(id="events", max_lines=200)
        yield self._events
        self._command = Input(placeholder="centrix …", id="command")
        self._command.display = False
        yield self._command
        yield Footer()

    async def on_mount(self) -> None:
        log_event("tui", "startup", "control surface mounted")
        self.refresh_state()
        self.refresh_events()
        self.set_interval(1.0, self.refresh_events)
        self.set_interval(2.0, self.refresh_state)

    def refresh_state(self) -> None:
        state = read_state()
        mode_value = "mock" if state.get("mode_mock", True) else state.get("mode", "real")
        paused_bool = bool(state.get("paused", False))
        paused_value = "1" if paused_bool else "0"
        running = self._running_services()
        kpi = snapshot_kpis()
        errors_1m = kpi.get("errors_1m", 0)
        alerts_dedup = kpi.get("alerts_dedup_1m", 0)
        status_text = (
            f"mode={mode_value} paused={paused_value} services={running}/{self._service_total} "
            f"errors_1m={errors_1m} alerts_dedup={alerts_dedup}"
        )
        if self._status is not None:
            self._status.update(status_text)

    def refresh_events(self) -> None:
        if self._events is None:
            return
        events = self._bus.tail_events(limit=50)
        new_events = [evt for evt in events if evt["id"] > self._last_event_id]
        if not new_events:
            return
        for event in new_events:
            timestamp = datetime.fromtimestamp(event["created_at"] / 1000).strftime("%H:%M:%S")
            payload = json.dumps(event["data"], separators=(",", ":"), ensure_ascii=False)
            line = f"{timestamp} [{event['level']}] {event['topic']} {payload}"
            self._events.write_line(line)
        self._last_event_id = max(evt["id"] for evt in new_events)

    def action_run(self) -> None:
        self._invoke_cli(["svc", "start", "all"], "svc.start", action="all")

    def action_stop(self) -> None:
        self._invoke_cli(["svc", "stop", "all"], "svc.stop", action="all")

    def action_mode(self) -> None:
        state = read_state()
        current = "mock" if state.get("mode_mock", True) else "real"
        target = "real" if current == "mock" else "mock"
        self._invoke_cli(["mode", "set", target], "mode.set", target=target)

    def action_pause(self) -> None:
        state = read_state()
        paused = bool(state.get("paused", False))
        command = "resume" if paused else "pause"
        self._invoke_cli(["state", command], "state.toggle", target=command)

    def action_order(self) -> None:
        self._invoke_cli(
            [
                "order",
                "new",
                "--symbol",
                "DEMO",
                "--qty",
                "1",
                "--px",
                "0",
            ],
            "order.new",
            symbol="DEMO",
        )

    def action_palette(self) -> None:
        if self._command is None:
            return
        self._command.display = True
        self._command.value = ""
        self.set_focus(self._command)

    def action_request_quit(self) -> None:
        log_event("tui", "shutdown", "quit requested")
        self.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is not self._command:
            return
        command = event.value.strip()
        if self._command is not None:
            self._command.display = False
        if self._status is not None:
            self.set_focus(self._status)
        if not command:
            return
        args = shlex.split(command)
        if args and args[0] == "centrix":
            args = args[1:]
        self._invoke_cli(args, "palette", raw=command)

    def _invoke_cli(self, args: list[str], topic: str, **fields: str) -> None:
        if not args:
            return
        command = [str(self._python_bin), "-m", "centrix.cli", *args]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        log_event(
            "tui",
            topic,
            "command executed",
            exit=result.returncode,
            args=" ".join(args),
            **fields,
        )
        if result.stdout:
            log_event("tui", f"{topic}.stdout", result.stdout.strip())
        if result.stderr:
            log_event("tui", f"{topic}.stderr", result.stderr.strip(), level="WARN")
        self.refresh_state()
        self.refresh_events()

    def _running_services(self) -> int:
        running = 0
        for name in SERVICE_NAMES:
            path = pidfile(name)
            if not path.exists():
                continue
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except ValueError:
                path.unlink(missing_ok=True)
                continue
            if is_running(pid):
                running += 1
            else:
                path.unlink(missing_ok=True)
        return running


def main() -> None:
    """Entrypoint used by systemd/tmux helpers."""

    ControlApp().run()


if __name__ == "__main__":
    main()
