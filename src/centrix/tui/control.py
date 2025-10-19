"""Textual-based TUI control surface."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Log, Static

from centrix.core.logging import ensure_runtime_dirs, get_text_logger
from centrix.ipc.bus import Bus
from centrix.settings import get_settings

DEFAULT_STATE = {
    "mode": "normal",
    "paused": "0",
    "breaker": "closed",
}


class ControlApp(App[None]):
    """Centrix control panel skeleton."""

    CSS = "#status { padding: 1 2; } #events { height: 1fr; padding: 1 2; }"

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+r", "run", "Run", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        Binding("ctrl+m", "mode", "Mode", priority=True),
        Binding("ctrl+p", "pause", "Pause", priority=True),
        Binding("ctrl+o", "order", "Order", priority=True),
        Binding("ctrl+k", "palette", "Palette", priority=True),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        ensure_runtime_dirs()
        self._app_logger: logging.Logger = get_text_logger("centrix.tui")
        self._bus = Bus(get_settings().ipc_db)
        self._status: Static | None = None
        self._events: Log | None = None
        self._last_event_id = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._status = Static("Centrix ready", id="status")
        yield self._status
        self._events = Log(id="events", max_lines=200)
        yield self._events
        yield Footer()

    async def on_mount(self) -> None:
        self._record("tui mounted")
        self.refresh_state()
        self.refresh_events()
        self.set_interval(1.0, self.refresh_events)
        self.set_interval(2.0, self.refresh_state)

    def refresh_state(self) -> None:
        state: dict[str, str] = {}
        for key, default in DEFAULT_STATE.items():
            value = self._bus.get_kv(key)
            if value is None:
                self._bus.set_kv(key, default)
                value = default
            state[key] = value
        paused = "yes" if state.get("paused") == "1" else "no"
        text = f"Mode: {state.get('mode')} | Paused: {paused} | Breaker: {state.get('breaker')}"
        if self._status is not None:
            self._status.update(text)

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
        self._bus.enqueue("control.run", {"source": "tui"})
        self._record("run requested")
        self.refresh_events()

    def action_stop(self) -> None:
        self._bus.emit("control.stop", "INFO", {"source": "tui"})
        self._record("stop requested")
        self.refresh_events()

    def action_mode(self) -> None:
        mode = self._bus.get_kv("mode") or DEFAULT_STATE["mode"]
        self._bus.emit("control.mode", "INFO", {"source": "tui", "mode": mode})
        self._record("mode toggle requested")
        self.refresh_events()

    def action_pause(self) -> None:
        paused = self._bus.get_kv("paused") == "1"
        new_paused = "0" if paused else "1"
        self._bus.set_kv("paused", new_paused)
        topic = "state.resume" if paused else "state.pause"
        self._bus.emit(topic, "INFO", {"source": "tui"})
        self._record("pause toggled" if paused else "pause requested")
        self.refresh_state()
        self.refresh_events()

    def action_order(self) -> None:
        self._bus.enqueue("control.order", {"source": "tui"})
        self._record("order flow requested")
        self.refresh_events()

    def action_palette(self) -> None:
        self._bus.emit("control.palette", "INFO", {"source": "tui"})
        self._record("palette toggled")
        toggle = getattr(self, "action_toggle_command_palette", None)
        if callable(toggle):
            toggle()
        self.refresh_events()

    def action_request_quit(self) -> None:
        self._bus.emit("control.quit", "INFO", {"source": "tui"})
        self._record("quit requested")
        self.exit()

    def _record(self, message: str) -> None:
        self._app_logger.info(message)
        if self._status is not None:
            self._status.update(message)


def main() -> None:
    """Entrypoint used by systemd/tmux helpers."""

    ControlApp().run()


if __name__ == "__main__":
    main()
