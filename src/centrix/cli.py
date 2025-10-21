"""Centrix command-line control interface."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request

import typer

from centrix.core import orders
from centrix.core.alerts import alert_counters, emit_alert
from centrix.core.logging import REPORT_DIR, TEXT_LOG, ensure_runtime_dirs, log_event
from centrix.core.metrics import snapshot_kpis
from centrix.ipc import Bus, is_running, pidfile, read_state, write_state
from centrix.settings import get_settings
from centrix.shared.locks import (
    acquire_lock,
    list_lock_files,
    lock_owner,
    reaper_sweep,
    release_lock,
)

from . import __version__

app = typer.Typer(name="centrix", help="Centrix trading control platform CLI.")
svc_app = typer.Typer(help="Manage Centrix services.")
mode_app = typer.Typer(help="Manage execution mode.")
state_app = typer.Typer(help="Pause/resume orchestration state.")
order_app = typer.Typer(help="Submit control orders (stub).")
locks_app = typer.Typer(help="Inspect cooperative locks.")
diag_app = typer.Typer(help="Diagnostics and reporting.")
log_cli_app = typer.Typer(help="Log inspection utilities.")

app.add_typer(svc_app, name="svc")
app.add_typer(mode_app, name="mode")
app.add_typer(state_app, name="state")
app.add_typer(order_app, name="order")
app.add_typer(locks_app, name="locks")
app.add_typer(diag_app, name="diag")
app.add_typer(log_cli_app, name="log")

SETTINGS = get_settings()
SERVICE_MODULES = {
    "tui": "centrix.tui.control",
    "dashboard": "centrix.dashboard.server",
    "worker": "centrix.services.confirm_worker",
    "slack": "centrix.services.slack",
}
ALLOWED_TARGETS: list[str] = list(SERVICE_MODULES)
LOCK_NAME = "svc.control"
LOCK_TTL = 30
PYTHON_BIN = Path(".venv/bin/python") if Path(".venv/bin/python").exists() else Path(sys.executable)
DASHBOARD_LOG = Path("/tmp/ml_dashboard.log")

ensure_runtime_dirs()

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "CRITICAL": 50}


@app.command()
def version() -> None:
    """Print the Centrix version."""

    typer.echo(__version__)


@app.command("slack:selftest")
def slack_selftest_cmd(report: bool = True) -> None:
    """
    Führt realen Slack-Test aus: auth.test und Posts in alle konfigurierten Kanäle.
    Setze SLACK_ENABLED=1 und SLACK_SIMULATION=0. Bricht mit Code !=0 bei Fehlern ab.
    """

    from centrix.services.slack import slack_selftest

    result = slack_selftest()
    checks = result.get("checks", [])

    rows: list[tuple[str, str, str, str]] = []
    for entry in checks:
        ok_value = entry.get("ok")
        if ok_value is True:
            ok_text = "yes"
        elif ok_value is False:
            ok_text = "no"
        else:
            ok_text = "skip"
        detail = str(entry.get("detail") or "-").replace("\n", " ")
        rows.append(
            (
                str(entry.get("check") or "-"),
                str(entry.get("target") or "-"),
                ok_text,
                detail,
            )
        )

    headers = ("Check", "Target", "OK", "Detail")
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    header_line = " ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    divider = " ".join("-" * widths[idx] for idx in range(len(headers)))
    typer.echo(header_line)
    typer.echo(divider)
    for row in rows:
        line = " ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers)))
        typer.echo(line)
    typer.echo(divider)

    status = result.get("status") or ("PASS" if result.get("overall_ok") else "FAIL")
    typer.echo(f"RESULT: {status}")

    if report:
        latest_report = Path("runtime/reports/slack_selftest.json")
        latest_report.parent.mkdir(parents=True, exist_ok=True)
        latest_report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["report_latest_path"] = str(latest_report)

    if not result.get("precheck_ok"):
        raise typer.Exit(2)
    if not result.get("overall_ok"):
        raise typer.Exit(1)

    raise typer.Exit(0)


def _parse_targets(target: str) -> list[str]:
    value = target.strip().lower()
    if value == "all":
        return ALLOWED_TARGETS.copy()
    selected = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not selected:
        raise typer.BadParameter("target list cannot be empty")
    bad = [name for name in selected if name not in ALLOWED_TARGETS]
    if bad:
        allowed = ",".join(ALLOWED_TARGETS)
        raise typer.BadParameter(f"unknown target(s): {','.join(bad)}; allowed={allowed}")
    return [name for name in ALLOWED_TARGETS if name in selected]


@contextmanager
def _control_lock(action: str, target: Iterable[str]) -> Iterator[None]:
    names = ",".join(target)
    if not acquire_lock(LOCK_NAME, ttl=LOCK_TTL):
        owner = lock_owner(LOCK_NAME) or {}
        emit_alert(
            "ERROR",
            f"{action}.lock",
            "control lock busy",
            fingerprint=f"{LOCK_NAME}:{names}",
        )
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


def _normalise_level(level: str) -> str:
    upper = level.upper()
    return upper if upper in _LEVEL_ORDER else "INFO"


def _start_service(name: str) -> bool:
    existing = _read_pid(name)
    if existing and is_running(existing):
        return False
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    if name == "dashboard":
        command = [sys.executable, "-m", "centrix.dashboard.server"]
        DASHBOARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            with DASHBOARD_LOG.open("ab") as log_handle:
                process = subprocess.Popen(command, stdout=log_handle, stderr=log_handle, env=env)
        except OSError as exc:
            emit_alert(
                "CRITICAL",
                "svc.dashboard.start",
                "failed to start dashboard",
                fingerprint="svc:dashboard:start",
            )
            log_event(
                "cli",
                "svc.dashboard.start",
                "service spawn failed",
                level="ERROR",
                error=str(exc),
            )
            return False
        _write_pid(name, process.pid)
        ok, last_error = _wait_for_dashboard(timeout=8.0)
        result = "ok" if ok else "timeout"
        log_fields: dict[str, Any] = {"target": "dashboard", "result": result}
        if last_error:
            log_fields["error"] = last_error
        log_event("cli", "svc.wait", "dashboard readiness check", **log_fields)
        if not ok:
            _stop_service("dashboard")
            return False
        return True

    command = _service_command(name)
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
        )
    except OSError as exc:
        emit_alert(
            "CRITICAL",
            f"svc.{name}.start",
            f"failed to start service {name}",
            fingerprint=f"svc:{name}:start",
        )
        log_event(
            "cli",
            f"svc.{name}.start",
            "service spawn failed",
            level="ERROR",
            error=str(exc),
        )
        return False
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
    except (ProcessLookupError, PermissionError, OSError) as exc:
        emit_alert(
            "ERROR",
            f"svc.{name}.stop",
            f"failed to stop service {name}",
            fingerprint=f"svc:{name}:stop",
        )
        log_event(
            "cli",
            f"svc.{name}.stop",
            "service termination failed",
            level="ERROR",
            error=str(exc),
            pid=pid,
        )
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


def _service_snapshot() -> dict[str, dict[str, Any]]:
    bus = Bus(SETTINGS.ipc_db)
    return bus.get_services_status(ALLOWED_TARGETS)


def _wait_for_dashboard(timeout: float = 8.0) -> tuple[bool, str | None]:
    host = SETTINGS.dashboard_host
    port = SETTINGS.dashboard_port
    url = f"http://{host}:{port}/healthz"
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with request.urlopen(url, timeout=1) as response:
                if 200 <= response.status < 300:
                    return True, None
        except urlerror.URLError as exc:
            last_error = str(exc.reason) if getattr(exc, "reason", None) else str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc)
        time.sleep(0.25)
    return False, last_error


@svc_app.command("start")
def svc_start(target: str = typer.Argument("all", help="Service target.")) -> None:
    names = _parse_targets(target)
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
    names = _parse_targets(target)
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
    names = _parse_targets(target)
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


@svc_app.command("wait")
def svc_wait_command(
    target: str = typer.Argument("dashboard", help="Service target (dashboard only)."),
    timeout: float = typer.Option(8.0, "--timeout", "-t", help="Seconds to wait for readiness."),
) -> None:
    names = _parse_targets(target)
    if names != ["dashboard"]:
        typer.echo("svc wait currently supports the dashboard service only.")
        raise typer.Exit(1)
    ok, last_error = _wait_for_dashboard(timeout=timeout)
    result = "ok" if ok else "timeout"
    log_fields: dict[str, Any] = {"target": "dashboard", "result": result}
    if last_error:
        log_fields["error"] = last_error
    log_event("cli", "svc.wait", "wait command executed", **log_fields)
    if ok:
        typer.echo("dashboard ready")
        return
    typer.echo("dashboard wait timeout")
    _stop_service("dashboard")
    raise typer.Exit(1)


def _emit_log_stream(path: Path, lines: int, follow: bool) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if lines > 0:
                buffer = deque(handle, maxlen=max(lines, 0))
                for entry in buffer:
                    typer.echo(entry.rstrip("\n"))
            else:
                for entry in handle:
                    typer.echo(entry.rstrip("\n"))
            if not follow:
                return
            handle.seek(0, os.SEEK_END)
            try:
                while True:
                    entry = handle.readline()
                    if entry:
                        typer.echo(entry.rstrip("\n"))
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                return
    except FileNotFoundError:
        typer.echo(f"Log file not found: {path}")


@svc_app.command("logs")
def svc_logs(
    target: str = typer.Argument("dashboard", help="Service target (dashboard only)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream log output."),
    lines: int = typer.Option(100, "--lines", "-n", help="Show the last N lines (<=0 for all)."),
) -> None:
    names = _parse_targets(target)
    if names != ["dashboard"]:
        typer.echo("svc logs currently supports the dashboard service only.")
        raise typer.Exit(1)
    log_path = DASHBOARD_LOG if DASHBOARD_LOG.exists() else Path("runtime/logs/centrix.log")
    if not log_path.exists():
        typer.echo("no dashboard log available")
        return
    _emit_log_stream(log_path, lines, follow)


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
    orders.add_order(
        {
            "source": "cli",
            "symbol": symbol,
            "qty": qty,
            "px": px,
        }
    )
    typer.echo(
        json.dumps(
            {"status": "accepted", "symbol": symbol, "qty": qty, "px": px}, separators=(",", ":")
        )
    )


@locks_app.command("ls")
def locks_ls() -> None:
    entries = list_lock_files()
    if not entries:
        typer.echo("no locks")
        return
    header = f"{'name':<20} {'pid':>6} {'age_s':>10} {'ttl_s':>10} {'expired':>8}"
    typer.echo(header)
    now_ms = int(time.time() * 1000)
    for entry in entries:
        acquired = int(entry["acquired_at"])
        ttl_ms = int(entry["ttl_ms"])
        age_s = max(0.0, (now_ms - acquired) / 1000)
        ttl_s = ttl_ms / 1000 if ttl_ms else 0.0
        expired_text = "true" if entry["expired"] else "false"
        line = (
            f"{entry['name']:<20} {entry['pid']:>6} {age_s:>10.1f} "
            f"{ttl_s:>10.1f} {expired_text:>8}"
        )
        typer.echo(line)
    log_event("cli", "locks.ls", "listed lock files", count=len(entries))


@locks_app.command("reap")
def locks_reap() -> None:
    removed = reaper_sweep()
    typer.echo(f"removed={removed}")
    log_event("cli", "locks.reap", "reaper sweep executed", removed=removed)


@diag_app.command("snapshot")
def diag_snapshot() -> None:
    ensure_runtime_dirs()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now()
    filename = REPORT_DIR / f"diag_{timestamp.strftime('%Y%m%d_%H%M%S')}.json"
    data = {
        "ts": timestamp.isoformat(timespec="seconds"),
        "state": read_state(),
        "services": _service_snapshot(),
        "kpi": snapshot_kpis(),
        "alerts": alert_counters(),
        "orders": orders.list_orders(),
    }
    filename.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    log_event("cli", "diag.snapshot", "diagnostic snapshot written", path=str(filename))
    typer.echo(str(filename))


@log_cli_app.command("tail")
def log_tail(
    level: str = typer.Option("INFO", "--level", "-l", help="Minimum level to include."),
    lines: int = typer.Option(20, "--lines", "-n", help="Number of lines to display."),
) -> None:
    ensure_runtime_dirs()
    min_level = _normalise_level(level)
    if not TEXT_LOG.exists():
        typer.secho("log file not found", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)

    def _line_level(line: str) -> str:
        for part in line.split():
            if part.startswith("level="):
                return _normalise_level(part.split("=", 1)[1])
        return "INFO"

    with TEXT_LOG.open("r", encoding="utf-8") as handle:
        lines_buffer = [
            line.rstrip("\n")
            for line in handle.readlines()
            if _LEVEL_ORDER.get(_line_level(line), 0) >= _LEVEL_ORDER[min_level]
        ]
    for entry in lines_buffer[-lines:]:
        typer.echo(entry)
    log_event(
        "cli",
        "log.tail",
        "log tail executed",
        min_level=min_level,
        lines=lines,
        matched=len(lines_buffer),
    )


if __name__ == "__main__":
    app()
