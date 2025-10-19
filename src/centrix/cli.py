"""Command-line interface entry point for Centrix."""

from __future__ import annotations

import json
from typing import Any

import typer

from centrix.core.locks import acquire as lock_acquire
from centrix.core.locks import list_locks as lock_list
from centrix.core.locks import reap as lock_reap
from centrix.core.locks import release as lock_release
from centrix.core.logging import ensure_runtime_dirs, get_text_logger, log_json
from centrix.ipc.bus import Bus
from centrix.ipc.migrate import epoch_ms
from centrix.settings import get_settings

from . import __version__

app = typer.Typer(name="centrix", help="Centrix trading control platform CLI.")
ctl_app = typer.Typer(help="Interact with the command/event bus.")
locks_app = typer.Typer(help="Manage distributed locks.")
state_app = typer.Typer(help="Manage shared state values.")

app.add_typer(ctl_app, name="ctl")
app.add_typer(locks_app, name="locks")
app.add_typer(state_app, name="state")

settings = get_settings()
ensure_runtime_dirs()


def _bus() -> Bus:
    return Bus(settings.ipc_db)


def _parse_json(value: str, param: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("payload must be a JSON object")
        return parsed
    except (json.JSONDecodeError, ValueError) as exc:  # pragma: no cover - typer handles display
        raise typer.BadParameter(f"{param}: {exc}") from exc


@app.command()
def version() -> None:
    """Print the Centrix version."""

    typer.echo(__version__)


@ctl_app.command("enqueue")
def ctl_enqueue(
    cmd_type: str = typer.Option(..., "--type", help="Command type identifier."),
    payload: str = typer.Option(..., "--payload", help="JSON payload for the command."),
    corr_id: str | None = typer.Option(None, "--corr", help="Correlation identifier."),
) -> None:
    """Queue a command on the bus."""

    bus = _bus()
    parsed = _parse_json(payload, "payload")
    command_id = bus.enqueue(cmd_type, parsed, corr_id)
    typer.echo(f"command {command_id}")


@ctl_app.command("emit")
def ctl_emit(
    topic: str = typer.Option(..., "--topic", help="Event topic."),
    level: str = typer.Option("INFO", "--level", help="Event level."),
    data: str = typer.Option(..., "--data", help="JSON payload for the event."),
    corr_id: str | None = typer.Option(None, "--corr", help="Correlation identifier."),
) -> None:
    """Emit an event to the bus."""

    bus = _bus()
    parsed = _parse_json(data, "data")
    event_id = bus.emit(topic, level, parsed, corr_id)
    typer.echo(f"event {event_id}")


@ctl_app.command("tail")
def ctl_tail(
    limit: int = typer.Option(100, "--limit", min=1, help="Maximum number of events."),
    level: str | None = typer.Option(None, "--level", help="Filter by level."),
    topic: str | None = typer.Option(None, "--topic", help="Filter by topic."),
) -> None:
    """Tail the most recent events."""

    bus = _bus()
    events = bus.tail_events(limit=limit, level=level, topic=topic)
    for event in events:
        typer.echo(json.dumps(event, separators=(",", ":"), ensure_ascii=False))


@locks_app.command("ls")
def locks_ls() -> None:
    """List active locks."""

    locks = lock_list()
    if not locks:
        typer.echo("[]")
        return
    for entry in locks:
        typer.echo(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))


@locks_app.command("acquire")
def locks_acquire(
    name: str,
    owner: str = typer.Option(..., "--owner", help="Owner identifier."),
    ttl_sec: int = typer.Option(..., "--ttl", min=1, help="Time-to-live in seconds."),
) -> None:
    """Acquire a lock."""

    success = lock_acquire(name, owner, ttl_sec)
    typer.echo("acquired" if success else "busy")


@locks_app.command("release")
def locks_release(
    name: str,
    owner: str = typer.Option(..., "--owner", help="Owner identifier."),
) -> None:
    """Release a lock."""

    success = lock_release(name, owner)
    typer.echo("released" if success else "not-owner")


@locks_app.command("reap")
def locks_reap() -> None:
    """Reap expired locks."""

    removed = lock_reap(epoch_ms())
    typer.echo(str(removed))


DEFAULT_STATE = {
    "mode": "normal",
    "paused": "0",
    "breaker": "closed",
}


def _ensure_state_defaults(bus: Bus) -> dict[str, str]:
    state: dict[str, str] = {}
    for key, default in DEFAULT_STATE.items():
        value = bus.get_kv(key)
        if value is None:
            bus.set_kv(key, default)
            value = default
        state[key] = value
    return state


@state_app.command("get")
def state_get() -> None:
    """Read state values from the key-value store."""

    bus = _bus()
    state = _ensure_state_defaults(bus)
    typer.echo(json.dumps(state, separators=(",", ":"), ensure_ascii=False))


@state_app.command("set")
def state_set(command: str) -> None:
    """Update state values (MODE:<value>|PAUSE|RESUME|BREAKER:<value>)."""

    bus = _bus()
    command_upper = command.upper()
    logger = get_text_logger("centrix.state")

    if command_upper == "PAUSE":
        bus.set_kv("paused", "1")
        bus.emit("state.pause", "INFO", {"source": "cli"})
        logger.info("state paused")
        log_json("INFO", "state paused", source="cli")
    elif command_upper == "RESUME":
        bus.set_kv("paused", "0")
        bus.emit("state.resume", "INFO", {"source": "cli"})
        logger.info("state resumed")
        log_json("INFO", "state resumed", source="cli")
    elif command_upper.startswith("MODE:"):
        mode_value = command.split(":", 1)[1]
        bus.set_kv("mode", mode_value)
        bus.emit("state.mode", "INFO", {"mode": mode_value, "source": "cli"})
        logger.info("state mode set to %s", mode_value)
        log_json("INFO", "state mode updated", mode=mode_value, source="cli")
    elif command_upper.startswith("BREAKER:"):
        breaker_value = command.split(":", 1)[1]
        bus.set_kv("breaker", breaker_value)
        bus.emit("state.breaker", "INFO", {"breaker": breaker_value, "source": "cli"})
        logger.info("state breaker set to %s", breaker_value)
        log_json("INFO", "state breaker updated", breaker=breaker_value, source="cli")
    else:
        raise typer.BadParameter("command must be MODE:<value>, BREAKER:<value>, PAUSE, or RESUME")

    state = _ensure_state_defaults(bus)
    typer.echo(json.dumps(state, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    app()
