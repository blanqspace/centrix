"""Slack service providing simulation and socket-mode integrations."""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from centrix.cli import _parse_targets, _start_service, _stop_service
from centrix.core import orders
from centrix.core.approvals import confirm as approve_order
from centrix.core.approvals import reject as reject_order
from centrix.core.approvals import request_approval
from centrix.core.logging import ensure_runtime_dirs, log_event
from centrix.core.metrics import METRICS
from centrix.core.rbac import allow, role_of
from centrix.ipc import read_state, write_state
from centrix.ipc.bus import Bus
from centrix.settings import get_settings

SIM_LOG = Path("runtime/reports/slack_sim.jsonl")
_SLACK_OUT: SlackOut | None = None


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _channel_or_default(value: str | None, fallback: str) -> str:
    return value or fallback


@dataclass
class SlackOut:
    """Outbound Slack helper supporting simulation and real transport."""

    simulation: bool
    bot_token: str | None

    def __post_init__(self) -> None:
        self._client: Any | None = None
        if not self.simulation and self.bot_token:
            try:
                from slack_sdk.web import WebClient

                self._client = WebClient(token=self.bot_token)
            except Exception as exc:  # pragma: no cover - slack optional
                log_event(
                    "slack",
                    "transport",
                    "slack_sdk unavailable, switching to simulation",
                    level="WARN",
                    error=str(exc),
                )
                self.simulation = True
        ensure_runtime_dirs()
        if self.simulation:
            SIM_LOG.parent.mkdir(parents=True, exist_ok=True)

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a message either to simulation log or Slack."""

        payload = {
            "ts": _now_iso(),
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
            "blocks": blocks or [],
            "metadata": metadata or {},
        }
        if self.simulation:
            with SIM_LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
            log_event(
                "slack",
                "outbound",
                "simulated slack message",
                channel=channel,
                thread=thread_ts or "-",
            )
            return {"ok": True, "ts": payload["ts"], "channel": channel}

        if self._client is None:  # pragma: no cover - defensive
            raise RuntimeError("Slack WebClient not initialised.")
        response = self._client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            metadata=metadata,
        )
        log_event("slack", "outbound", "message posted", channel=channel)
        return dict(response.data)


def get_slack_out() -> SlackOut:
    """Return a module-wide SlackOut instance."""

    global _SLACK_OUT
    if _SLACK_OUT is not None:
        return _SLACK_OUT
    settings = get_settings()
    simulation = bool(settings.slack_simulation or not settings.slack_enabled)
    if settings.slack_enabled and settings.slack_bot_token and settings.slack_app_token:
        simulation = simulation or False
    else:
        simulation = True
    _SLACK_OUT = SlackOut(simulation=simulation, bot_token=settings.slack_bot_token)
    return _SLACK_OUT


def channel_for(kind: str) -> str:
    settings = get_settings()
    default_channel = settings.slack_channel_logs or "#centrix"
    mapping = {
        "control": settings.slack_channel_control or default_channel,
        "logs": settings.slack_channel_logs or default_channel,
        "alerts": settings.slack_channel_alerts or default_channel,
        "orders": settings.slack_channel_orders or default_channel,
    }
    return mapping.get(kind, default_channel)


def _service_status_summary() -> str:
    bus = Bus(get_settings().ipc_db)
    snapshot = bus.get_services_status(["tui", "dashboard", "worker", "slack"])
    parts = []
    for name, info in snapshot.items():
        status = "running" if info.get("running") else "stopped"
        parts.append(f"{name}:{status}")
    state = read_state()
    paused = "paused" if state.get("paused") else "active"
    mode = state.get("mode")
    parts.append(f"mode={mode}")
    parts.append(f"state={paused}")
    return ", ".join(parts)


def _ensure_role(action: str, user_id: str) -> tuple[bool, str]:
    role = role_of(user_id)
    if not allow(action, role):
        log_event(
            "slack",
            "rbac",
            "action denied",
            level="WARN",
            action=action,
            role=role,
            user=user_id,
        )
        return (False, f"Access denied for action '{action}' (role={role})")
    return (True, "")


def handle_slash_command(user_id: str, text: str) -> dict[str, Any]:
    """Handle `/cx` style slash commands."""

    parts = [segment for segment in text.strip().split() if segment]
    if not parts:
        return {"text": "Usage: /cx [status|pause|resume|mode|order|restart]"}

    action = parts[0].lower()
    ok, reason = _ensure_role(action if action != "mode" else "mode", user_id)
    if not ok:
        return {"text": reason}

    if action == "status":
        summary = _service_status_summary()
        get_slack_out().post_message(channel_for("control"), summary)
        METRICS.increment_counter("control.actions_total")
        return {"text": summary}

    if action == "pause":
        write_state(paused=True)
        get_slack_out().post_message(channel_for("control"), "System paused.")
        log_event("slack", "control.pause", "pause requested", user=user_id)
        METRICS.increment_counter("control.actions_total")
        return {"text": "Paused orchestration."}

    if action == "resume":
        write_state(paused=False)
        get_slack_out().post_message(channel_for("control"), "System resumed.")
        log_event("slack", "control.resume", "resume requested", user=user_id)
        METRICS.increment_counter("control.actions_total")
        return {"text": "Resumed orchestration."}

    if action == "mode":
        target = parts[1].lower() if len(parts) > 1 else None
        current = read_state()
        desired = target or ("real" if current.get("mode") == "mock" else "mock")
        write_state(mode=desired, mode_mock=(desired == "mock"))
        get_slack_out().post_message(channel_for("control"), f"Mode set to {desired}.")
        log_event("slack", "control.mode", "mode updated", user=user_id, mode=desired)
        METRICS.increment_counter("control.actions_total")
        return {"text": f"Switched mode to {desired}."}

    if action == "restart":
        ok, reason = _ensure_role("restart", user_id)
        if not ok:
            return {"text": reason}
        if len(parts) < 2:
            return {"text": "Usage: /cx restart [tui|dashboard|worker|slack]"}
        target = parts[1].lower()
        try:
            targets = _parse_targets(target)
        except Exception as exc:
            return {"text": str(exc)}
        report: list[str] = []
        for name in targets:
            stopped = _stop_service(name)
            started = _start_service(name)
            report.append(f"{name}: stop={stopped} start={started}")
        message = "Restart summary: " + ", ".join(report)
        get_slack_out().post_message(channel_for("control"), message)
        log_event(
            "slack",
            "control.restart",
            "service restart requested",
            user=user_id,
            summary=message,
        )
        METRICS.increment_counter("control.actions_total")
        return {"text": message}

    if action == "order":
        ok, reason = _ensure_role("order", user_id)
        if not ok:
            return {"text": reason}
        if len(parts) < 4:
            return {"text": "Usage: /cx order SYMBOL QTY PX"}
        symbol = parts[1]
        try:
            qty = int(parts[2])
            px = float(parts[3])
        except ValueError:
            return {"text": "Quantity must be int, price float."}
        settings = get_settings()
        bus = Bus(settings.ipc_db)
        order_id = bus.enqueue(
            "order.submit",
            {"symbol": symbol, "qty": qty, "px": px, "source": "slack", "user": user_id},
        )
        orders.add_order(
            {"source": "slack", "symbol": symbol, "qty": qty, "px": px, "user": user_id}
        )
        token = request_approval(
            order_id,
            initiator=user_id,
            ttl_s=settings.order_approval_ttl_sec,
        )
        message = (
            f"Order request #{order_id} {symbol} qty={qty} px={px} "
            f"initiated by <@{user_id}>. Token: {token}"
        )
        get_slack_out().post_message(
            channel_for("orders"),
            message,
            metadata={"order_id": order_id, "token": token, "type": "order.submit"},
        )
        log_event(
            "slack",
            "order.new",
            "order submitted via slack",
            user=user_id,
            symbol=symbol,
            qty=qty,
            px=px,
            order_id=order_id,
        )
        METRICS.increment_counter("control.actions_total")
        return {"text": f"Order #{order_id} queued. Awaiting confirmation token {token}."}

    return {"text": f"Unknown subcommand '{action}'."}


def handle_button(
    action_id: str,
    *,
    user: str,
    order_id: int,
    token: str | None = None,
) -> dict[str, Any]:
    """Process interactive button callbacks."""

    if action_id == "confirm":
        ok, reason = _ensure_role("confirm", user)
        if not ok:
            return {"text": reason}
        if not token:
            return {"text": "Missing approval token."}
        success, msg = approve_order(order_id, approver=user, token=token)
        if success:
            text = f"Order #{order_id} confirmed by <@{user}>."
            channel = channel_for("orders")
            get_slack_out().post_message(channel, text)
            log_event("slack", "order.confirm", "order confirmed", user=user, order_id=order_id)
            return {"text": text}
        return {"text": msg}

    if action_id == "reject":
        ok, reason = _ensure_role("reject", user)
        if not ok:
            return {"text": reason}
        success, msg = reject_order(order_id, approver=user, reason="Rejected via Slack")
        if success:
            text = f"Order #{order_id} rejected by <@{user}>."
            get_slack_out().post_message(channel_for("orders"), text)
            log_event("slack", "order.reject", "order rejected", user=user, order_id=order_id)
            return {"text": text}
        return {"text": msg}

    return {"text": f"Unhandled action '{action_id}'."}


def route_alert(level: str, topic: str, message: str, **fields: Any) -> None:
    """Dispatch alerts to Slack once minimum level is met."""

    settings = get_settings()
    if not settings.slack_enabled:
        return
    levels = ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    try:
        min_idx = levels.index(settings.alert_min_level.upper())
        level_idx = levels.index(level.upper())
    except ValueError:
        return
    if level_idx < min_idx:
        return
    payload = f"[{level.upper()}] {topic}: {message}"
    metadata = {"fields": fields, "topic": topic, "level": level}
    get_slack_out().post_message(channel_for("alerts"), payload, metadata=metadata)


class SlackService:
    """Slack service runtime handling simulation or socket mode."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.out = get_slack_out()

    def _run_simulation(self) -> None:
        log_event("slack", "startup", "slack service started (simulation)", mode="sim")
        interval = 15
        while True:
            log_event("slack", "heartbeat", "slack simulation alive")
            time.sleep(interval)

    def _run_socket_mode(self) -> None:  # pragma: no cover - requires slack_sdk
        try:
            import slack_sdk  # noqa: F401
        except Exception as exc:
            log_event(
                "slack",
                "socket_mode",
                "slack_sdk unavailable, using simulation",
                level="WARN",
                error=str(exc),
            )
            self.out.simulation = True
            self._run_simulation()
            return

        log_event(
            "slack",
            "socket_mode",
            "socket-mode support deferred to simulation in this build",
            level="WARN",
        )
        self._run_simulation()

    def run(self) -> None:
        ensure_runtime_dirs()
        _install_signal_handlers()
        if self.out.simulation:
            self._run_simulation()
        else:
            self._run_socket_mode()


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))


def run() -> None:
    SlackService().run()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
