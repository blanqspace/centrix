"""Textual-based TUI control surface placeholder."""
from __future__ import annotations

import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static

LOG_PATH = Path("runtime/logs/centrix.log")


def _get_logger() -> logging.Logger:
    """Configure a file-backed logger for the TUI."""

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("centrix.tui")
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class ControlApp(App[None]):
    """Centrix control panel skeleton."""

    CSS = "#status { padding: 1 2; }"

    BINDINGS = [
        Binding("ctrl+r", "run", "Run", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        Binding("ctrl+m", "mode", "Mode", priority=True),
        Binding("ctrl+p", "pause", "Pause", priority=True),
        Binding("ctrl+o", "order", "Order", priority=True),
        Binding("ctrl+k", "palette", "Palette", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._logger = _get_logger()
        self._status: Static | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._status = Static("Centrix ready", id="status")
        yield self._status
        yield Footer()

    def on_mount(self) -> None:  # type: ignore[override]
        self._log("tui mounted")

    def _log(self, message: str) -> None:
        self._logger.info(message)
        if self._status is not None:
            self._status.update(f"{message}")

    def action_run(self) -> None:
        self._log("run requested")

    def action_stop(self) -> None:
        self._log("stop requested")

    def action_mode(self) -> None:
        self._log("mode toggle requested")

    def action_pause(self) -> None:
        self._log("pause requested")

    def action_order(self) -> None:
        self._log("order flow requested")

    def action_palette(self) -> None:
        self._log("palette toggled")
        toggle = getattr(self, "action_toggle_command_palette", None)
        if callable(toggle):
            toggle()

    def action_quit(self) -> None:
        self._log("quit requested")
        self.exit()


def main() -> None:
    """Entrypoint used by systemd/tmux helpers."""

    ControlApp().run()


if __name__ == "__main__":
    main()
