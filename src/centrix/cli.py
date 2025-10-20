"""Centrix command-line control interface."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from centrix.core.logging import ensure_runtime_dirs, log_event
from centrix.ipc import is_running, pidfile, read_state, write_state
from centrix.settings import get_settings
from centrix.shared.locks import acquire_lock, lock_owner, release_lock

from . import __version__

app = typer.Typer(name="centrix", help="Centrix trading control platform CLI.")
svc_app = typer.Typer(help="Manage Centrix services.")
mode_app = typer.Typer(help="Manage execution mode.")
state_app = typer.Typer(help="Pause/resume orchestration state.")
order_app = typer.Typer(help="Submit control orders (stub).")

app.add_typer(svc_app, name="svc")
app.add_typer(mode_app, name="mode")
app.add_typer(state_app, name="state")
app.add_typer(order_app, name="order")

SETTINGS = get_settings()
SERVICE_MODULES = {
    "tui": "centrix.tui.control",
    "dashboard": "centrix.dashboard.server",
    "worker": "centrix.services.confirm_worker",
}
SERVICE_NAMES: list[str] = list(SERVICE_MODULES)
LOCK_NAME = "svc.control"
LOCK_TTL = 30
PYTHON_BIN = Path(".venv/bin/python") if Path(".venv/bin/python").exists() else Path(sys.executable)

ensure_runtime_dirs()


@app.command()
def version() -> None:
    """Print the Centrix version."""

    typer.echo(__version__)


def _resolve_targets(target: str) -> list[str]:
    target_lower = target.lower()
    if target_lower == "all":
        return SERVICE_NAMES.copy()
    if target_lower not in SERVICE_MODULES:
        raise typer.BadParameter(
            "target must be one of tui|dashboard|worker|all",
        )
    return [target_lower]


@contextmanager
def _control_lock(action: str, target: Iterable[str]) -> Iterator[None]:
    names = ",".join(target)
    if not acquire_lock(LOCK_NAME, ttl=LOCK_TTL):
        owner = lock_owner(LOCK_NAME) or {}
        log_event(
            "cli",
            f"{action}.lock",
            "control lock busy",
            level="WARN",
            target=names,
            owner=owner.get("pid", "?"),
        )
        typer.secho("control operations are busy", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        yield
    finally:
        release_lock(LOCK_NAME)


def _service_command(name: str) -> list[str]:
    module = SERVICE_MODULES[name]
    return [str(PYTHON_BIN), "-m", module]


def _write_pid(name: str, pid: int) -> None:
    path = pidfile(name)
    path.write_text(str(pid), encoding="utf-8")


def _read_pid(name: str) -> int | None:
    path = pidfile(name)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return None


def _start_service(name: str) -> bool:
    existing = _read_pid(name)
    if existing and is_running(existing):
        return False
    command = _service_command(name)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
    )
    _write_pid(name, process.pid)
    return True


def _stop_service(name: str) -> bool:
    pid = _read_pid(name)
    if pid is None:
        return False
    if not is_running(pid):
        pidfile(name).unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pidfile(name).unlink(missing_ok=True)
        return False
    timeout = time.time() + 5
    while time.time() < timeout:
        if not is_running(pid):
            pidfile(name).unlink(missing_ok=True)
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pidfile(name).unlink(missing_ok=True)
    return True


def _status(name: str) -> tuple[str, bool]:
    pid = _read_pid(name)
    if pid and is_running(pid):
        return (f"{name}: running pid={pid}", True)
    return (f"{name}: stopped", False)


@svc_app.command("start")
def svc_start(target: str = typer.Argument("all", help="Service target.")) -> None:
    names = _resolve_targets(target)
    with _control_lock("svc.start", names):
        started = [name for name in names if _start_service(name)]
        skipped = [name for name in names if name not in started]
    log_event(
        "cli",
        "svc.start",
        "start command executed",
        started=",".join(started) or "-",
        skipped=",".join(skipped) or "-",
    )
    started_text = ", ".join(started) if started else "-"
    skipped_text = ", ".join(skipped) if skipped else "-"
    typer.echo(f"Started: {started_text}; Already running: {skipped_text}")


@svc_app.command("stop")
def svc_stop(target: str = typer.Argument("all", help="Service target.")) -> None:
    names = _resolve_targets(target)
    with _control_lock("svc.stop", names):
        stopped = [name for name in names if _stop_service(name)]
        idle = [name for name in names if name not in stopped]
    log_event(
        "cli",
        "svc.stop",
        "stop command executed",
        stopped=",".join(stopped) or "-",
        idle=",".join(idle) or "-",
    )
    stopped_text = ", ".join(stopped) if stopped else "-"
    idle_text = ", ".join(idle) if idle else "-"
    typer.echo(f"Stopped: {stopped_text}; Already stopped: {idle_text}")


@svc_app.command("status")
def svc_status(target: str = typer.Argument("all", help="Service target.")) -> None:
    names = _resolve_targets(target)
    lines = []
    running = 0
    for name in names:
        line, is_running_flag = _status(name)
        lines.append(line)
        if is_running_flag:
            running += 1
    for line in lines:
        typer.echo(line)
    summary = f"Summary: running={running}/{len(names)}"
    typer.echo(summary)
    log_event("cli", "svc.status", "status command executed", running=running, total=len(names))


@mode_app.command("set")
def mode_set(value: str = typer.Argument(..., help="mock|real")) -> None:
    mode_value = value.lower()
    if mode_value not in {"mock", "real"}:
        raise typer.BadParameter("mode must be mock or real")
    state = write_state(mode=mode_value, mode_mock=(mode_value == "mock"))
    typer.echo(
        json.dumps(
            {"mode": state.get("mode"), "mode_mock": state.get("mode_mock")}, separators=(",", ":")
        )
    )
    log_event("cli", "mode.set", "mode updated", mode=mode_value)


@mode_app.command("status")
def mode_status() -> None:
    state = read_state()
    typer.echo(
        json.dumps(
            {"mode": state.get("mode"), "mode_mock": state.get("mode_mock")}, separators=(",", ":")
        )
    )
    log_event("cli", "mode.status", "mode status queried", mode=state.get("mode"))


@state_app.command("pause")
def state_pause() -> None:
    state = write_state(paused=True)
    typer.echo(json.dumps({"paused": state.get("paused")}, separators=(",", ":")))
    log_event("cli", "state.pause", "state paused")


@state_app.command("resume")
def state_resume() -> None:
    state = write_state(paused=False)
    typer.echo(json.dumps({"paused": state.get("paused")}, separators=(",", ":")))
    log_event("cli", "state.resume", "state resumed")


@state_app.command("status")
def state_status() -> None:
    state = read_state()
    typer.echo(
        json.dumps(
            {"paused": state.get("paused"), "mode": state.get("mode")}, separators=(",", ":")
        )
    )
    log_event("cli", "state.status", "state status queried", paused=state.get("paused"))


@order_app.command("new")
def order_new(
    symbol: str = typer.Option(..., "--symbol", help="Symbol identifier."),
    qty: int = typer.Option(..., "--qty", min=1, help="Quantity."),
    px: float = typer.Option(..., "--px", help="Price."),
) -> None:
    log_event("cli", "order.new", "stub order accepted", symbol=symbol, qty=qty, px=px)
    typer.echo(
        json.dumps(
            {"status": "accepted", "symbol": symbol, "qty": qty, "px": px}, separators=(",", ":")
        )
    )


if __name__ == "__main__":
    app()
