"""Slack service providing simulation and socket-mode integrations."""

from __future__ import annotations

import json
import signal
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.web import WebClient
from slack_sdk.web.slack_response import SlackResponse

from centrix.cli import _parse_targets, _start_service, _stop_service
from centrix.core import orders
from centrix.core.approvals import confirm as approve_order
from centrix.core.approvals import reject as reject_order
from centrix.core.approvals import request_approval
from centrix.core.logging import ensure_runtime_dirs, log_event, warn_on_local_env
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


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    prefixes = ("xoxb-", "xapp-", "xoxp-", "xoxa-", "xoxr-")
    for prefix in prefixes:
        if value.startswith(prefix):
            return prefix + "****"
    return value[:4] + "****"


def _log_stage(step: str, message: str, **fields: Any) -> None:
    log_event("slack", "selftest", message, level="INFO", step=step, **fields)


def _log_error(step: str, code: str, message: str, **fields: Any) -> None:
    log_event("slack", "selftest", message, level="ERROR", step=step, error=code, **fields)


def slack_env_summary() -> dict[str, Any]:
    settings = get_settings()
    channels_present = {
        "control": bool(settings.slack_channel_control),
        "logs": bool(settings.slack_channel_logs),
        "alerts": bool(settings.slack_channel_alerts),
        "orders": bool(settings.slack_channel_orders),
    }
    return {
        "enabled": bool(settings.slack_enabled),
        "simulation": bool(settings.slack_simulation),
        "has_app_token": bool(settings.slack_app_token),
        "has_bot_token": bool(settings.slack_bot_token),
        "has_signing": bool(settings.slack_signing_secret),
        "channels_present": channels_present,
        "role_map_count": len(settings.slack_role_map or {}),
    }


def slack_auth_test(client: WebClient) -> dict[str, Any]:
    try:
        response = client.auth_test()
        if isinstance(response, SlackResponse):
            data = cast(dict[str, Any], response.data)
        elif isinstance(response, dict):
            data = cast(dict[str, Any], response)
        else:
            data = {}
        ok = bool(data.get("ok"))
        return {
            "ok": ok,
            "user_id": data.get("user_id"),
            "team": data.get("team"),
            "error": None if ok else data.get("error"),
            "code": None if ok else data.get("error"),
        }
    except (SlackApiError, SlackClientError) as exc:
        error_detail = None
        error_code = None
        error_response = getattr(exc, "response", None)
        response_data: Any = None
        if isinstance(error_response, SlackResponse):
            response_data = error_response.data
        else:
            response_data = getattr(error_response, "data", None) or getattr(
                error_response, "body", None
            )
        if isinstance(response_data, dict):
            error_detail = response_data.get("error") or response_data.get("detail")
            error_code = response_data.get("error")
        if not error_detail:
            error_detail = str(exc)
        if not error_code:
            error_code = "exception"
        return {
            "ok": False,
            "user_id": None,
            "team": None,
            "error": error_detail,
            "code": error_code,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "user_id": None,
            "team": None,
            "error": str(exc),
            "code": "exception",
        }


def socket_mode_probe() -> dict[str, Any]:
    settings = get_settings()
    if not settings.slack_enabled:
        return {"ok": False, "error": "slack disabled", "code": "slack_disabled"}
    if settings.slack_simulation:
        return {"ok": False, "error": "simulation mode active", "code": "simulation_active"}
    if not settings.slack_app_token:
        return {"ok": False, "error": "missing app token", "code": "missing_app_token"}
    if not settings.slack_bot_token:
        return {"ok": False, "error": "missing bot token", "code": "missing_bot_token"}
    try:
        client = SocketModeClient(
            app_token=settings.slack_app_token,
            web_client=WebClient(token=settings.slack_bot_token),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "code": "init_error"}
    try:
        client.connect()
        time.sleep(0.5)
        connected = getattr(client, "connected", True)
        if not connected:
            return {"ok": False, "error": "socket client did not connect", "code": "not_connected"}
        return {"ok": True, "error": None, "code": None}
    except Exception as exc:  # pragma: no cover - socket optional
        return {"ok": False, "error": str(exc), "code": "exception"}
    finally:
        try:
            cast(Any, client).close()
        except Exception:
            pass


def post_probe(client: WebClient, channel: str, text: str) -> dict[str, Any]:
    try:
        response = client.chat_postMessage(channel=channel, text=text)
        if isinstance(response, SlackResponse):
            data = cast(dict[str, Any], response.data)
        elif isinstance(response, dict):
            data = cast(dict[str, Any], response)
        else:
            data = {}
        ok = bool(data.get("ok"))
        return {
            "ok": ok,
            "ts": data.get("ts"),
            "channel": channel,
            "error": None if ok else data.get("error"),
            "code": None if ok else data.get("error"),
        }
    except (SlackApiError, SlackClientError) as exc:
        error_detail = None
        error_code = None
        error_response = getattr(exc, "response", None)
        response_data: Any = None
        if isinstance(error_response, SlackResponse):
            response_data = error_response.data
        else:
            response_data = getattr(error_response, "data", None) or getattr(
                error_response, "body", None
            )
        if isinstance(response_data, dict):
            error_detail = response_data.get("error") or response_data.get("detail")
            error_code = response_data.get("error")
        if not error_detail:
            error_detail = str(exc)
        if not error_code:
            error_code = "exception"
        return {
            "ok": False,
            "ts": None,
            "channel": channel,
            "error": error_detail,
            "code": error_code,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "ts": None,
            "channel": channel,
            "error": str(exc),
            "code": "exception",
        }


def slack_selftest() -> dict[str, Any]:
    ensure_runtime_dirs()
    run_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    _log_stage("start", "starting slack selftest", ts=run_at)

    settings = get_settings()
    env = slack_env_summary()
    channels_configured = {
        "control": settings.slack_channel_control,
        "logs": settings.slack_channel_logs,
        "alerts": settings.slack_channel_alerts,
        "orders": settings.slack_channel_orders,
    }
    masked_tokens = {
        "bot_token": _mask_secret(settings.slack_bot_token),
        "app_token": _mask_secret(settings.slack_app_token),
        "signing_secret": _mask_secret(settings.slack_signing_secret),
    }

    channel_pairs = [
        f"{kind}:{channel}" for kind, channel in channels_configured.items() if channel
    ]
    env_detail_base = (
        f"bot={masked_tokens['bot_token'] or '-'} "
        f"app={masked_tokens['app_token'] or '-'} "
        f"signing={'yes' if env['has_signing'] else 'no'} "
        f"channels={','.join(channel_pairs) if channel_pairs else '-'}"
    )

    precheck_failures: list[tuple[str, str]] = []
    if not env["enabled"]:
        precheck_failures.append(
            ("slack_disabled", "Slack integration disabled (SLACK_ENABLED must be 1)")
        )
    if env["simulation"]:
        precheck_failures.append(
            ("simulation_active", "Slack simulation mode active (set SLACK_SIMULATION=0)")
        )
    if not env["has_bot_token"]:
        precheck_failures.append(("missing_bot_token", "Missing bot token (SLACK_BOT_TOKEN)"))
    if not env["has_app_token"]:
        precheck_failures.append(("missing_app_token", "Missing app token (SLACK_APP_TOKEN)"))
    if not env["has_signing"]:
        precheck_failures.append(
            ("missing_signing_secret", "Missing signing secret (SLACK_SIGNING_SECRET)")
        )

    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    channel_results: list[dict[str, Any]] = []

    def record_check(
        step: str,
        target: str,
        ok: bool | None,
        detail: str,
        *,
        code: str | None = None,
        label: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "check": label or step,
            "target": target,
            "ok": ok,
            "detail": detail,
        }
        if code:
            entry["code"] = code
        if extra:
            entry.update(extra)
        checks.append(entry)
        status_map = {True: "ok", False: "failed", None: "skipped"}
        status = status_map.get(ok, "unknown")
        log_fields = {"target": target, "detail": detail, "ok": ok}
        if extra:
            log_fields.update(extra)
        _log_stage(step, f"{step} {status}", **log_fields)
        if ok is False:
            error_code = code or "failure"
            errors.append(
                {
                    "step": step,
                    "target": target,
                    "code": error_code,
                    "message": detail,
                    **(extra or {}),
                }
            )
            _log_error(
                step,
                error_code,
                f"{step} failed",
                target=target,
                detail=detail,
                **(extra or {}),
            )

    precheck_ok = not precheck_failures
    if precheck_ok:
        record_check("environment", "tokens/channels", True, env_detail_base, label="env")
    else:
        failure_detail = "; ".join(f"error={code}: {msg}" for code, msg in precheck_failures)
        record_check(
            "environment",
            "tokens/channels",
            False,
            f"{env_detail_base}; {failure_detail}",
            code=precheck_failures[0][0],
            label="env",
        )

    client: Any | None = None
    auth_result: dict[str, Any] = {
        "ok": None,
        "user_id": None,
        "team": None,
        "error": "skipped",
        "code": "skipped",
    }
    if precheck_ok:
        client = WebClient(token=settings.slack_bot_token)
        auth_result = slack_auth_test(client)
        if auth_result["ok"]:
            detail = f"user={auth_result.get('user_id')} team={auth_result.get('team')}"
            record_check("auth.test", "api.slack.com", True, detail, label="auth.test")
        else:
            detail = f"error={auth_result.get('code')}: {auth_result.get('error')}"
            record_check(
                "auth.test",
                "api.slack.com",
                False,
                detail,
                code=auth_result.get("code") or "auth_failed",
                label="auth.test",
            )
    else:
        record_check(
            "auth.test", "api.slack.com", None, "skipped (precondition failed)", label="auth.test"
        )

    probe_text = f"Centrix selftest OK {run_at}"
    if client and auth_result.get("ok"):
        for kind, channel in channels_configured.items():
            if not channel:
                record_check(
                    "post",
                    kind.upper(),
                    None,
                    "skipped (not configured)",
                    label="post",
                    extra={"kind": kind, "channel": None},
                )
                channel_results.append(
                    {
                        "kind": kind,
                        "channel": None,
                        "ok": None,
                        "detail": "skipped (not configured)",
                    }
                )
                continue
            post_result = post_probe(client, channel, probe_text)
            if post_result["ok"]:
                detail = f"ts={post_result.get('ts')}"
                record_check(
                    "post",
                    kind.upper(),
                    True,
                    detail,
                    label="post",
                    extra={"kind": kind, "channel": channel},
                )
            else:
                detail = f"error={post_result.get('code')}: {post_result.get('error')}"
                record_check(
                    "post",
                    kind.upper(),
                    False,
                    detail,
                    code=post_result.get("code") or "post_failed",
                    label="post",
                    extra={"kind": kind, "channel": channel},
                )
            channel_results.append(
                {
                    "kind": kind,
                    "channel": channel,
                    "ok": post_result["ok"],
                    "ts": post_result.get("ts"),
                    "error": post_result.get("error"),
                    "code": post_result.get("code"),
                    "detail": detail,
                }
            )
    else:
        for kind, channel in channels_configured.items():
            if channel:
                msg = "skipped (auth.test failed)"
            else:
                msg = "skipped (not configured)"
            record_check(
                "post",
                kind.upper(),
                None,
                msg,
                label="post",
                extra={"kind": kind, "channel": channel},
            )
            channel_results.append(
                {
                    "kind": kind,
                    "channel": channel,
                    "ok": None,
                    "detail": msg,
                }
            )

    socket_result: dict[str, Any] = {"ok": None, "error": "skipped", "code": "skipped"}
    if precheck_ok:
        if env["has_app_token"]:
            socket_result = socket_mode_probe()
            if socket_result["ok"]:
                record_check(
                    "socket-mode",
                    "xapp handshake",
                    True,
                    "connected",
                    label="socket-mode",
                )
            else:
                detail = f"error={socket_result.get('code')}: {socket_result.get('error')}"
                record_check(
                    "socket-mode",
                    "xapp handshake",
                    False,
                    detail,
                    code=socket_result.get("code") or "socket_failed",
                    label="socket-mode",
                )
        else:
            record_check(
                "socket-mode",
                "xapp handshake",
                None,
                "skipped (missing app token)",
                label="socket-mode",
            )
    else:
        record_check(
            "socket-mode",
            "xapp handshake",
            None,
            "skipped (precondition failed)",
            label="socket-mode",
        )

    posts_ok = all(
        entry.get("ok") in (True, None) for entry in channel_results if entry.get("channel")
    )
    socket_ok = socket_result.get("ok") in (True, None)
    overall_ok = bool(precheck_ok and auth_result.get("ok") and posts_ok and socket_ok)

    status = "PASS" if overall_ok else "FAIL"
    _log_stage("result", f"selftest result {status.lower()}", status=status, errors=len(errors))

    report_payload = {
        "run_at": run_at,
        "precheck_ok": precheck_ok,
        "precheck_failures": precheck_failures,
        "overall_ok": overall_ok,
        "status": status,
        "summary": env,
        "checks": checks,
        "auth": auth_result,
        "channels": channel_results,
        "socket_mode": socket_result,
        "errors": errors,
        "masked_tokens": masked_tokens,
    }

    report_dir = Path("runtime/reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"slack_selftest_{run_at.replace(':', '').replace('-', '')}.json"
    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")
    report_payload["report_path"] = str(report_path)
    _log_stage("finish", "selftest finished", status=status, report=str(report_path))
    return report_payload


@dataclass
class SlackOut:
    """Outbound Slack helper supporting simulation and real transport."""

    simulation: bool
    bot_token: str | None

    def __post_init__(self) -> None:
        self._client: Any | None = None
        if not self.simulation and self.bot_token:
            try:
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
    levels = ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    if not settings.slack_enabled:
        return
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


def process_slash_command_request(
    body: Mapping[str, Any] | None,
    ack: Callable[[], Any],
    respond: Callable[[str], Any],
    *,
    handler: Callable[..., dict[str, Any]] = handle_slash_command,
) -> None:
    """Wrap slash command handling ensuring immediate acknowledgement."""

    ack()
    payload = dict(body) if isinstance(body, Mapping) else {}
    user_id = str(payload.get("user_id") or "")
    text_raw = payload.get("text")
    text = str(text_raw) if text_raw is not None else ""
    try:
        response = handler(user_id=user_id, text=text)
        message = str(response.get("text") or "ok")
    except Exception as exc:  # pragma: no cover - defensive
        log_event("slack", "handler", "cx error", level="ERROR", error=str(exc))
        respond("error processing command")
        return
    respond(message)


def process_action_request(
    action_id: str,
    body: Mapping[str, Any] | None,
    ack: Callable[[], Any],
    respond: Callable[[str], Any],
    *,
    handler: Callable[..., dict[str, Any]] = handle_button,
) -> None:
    """Wrap interactive action handling extracting metadata safely."""

    ack()
    payload = dict(body) if isinstance(body, Mapping) else {}
    user_section = payload.get("user")
    user_id = ""
    if isinstance(user_section, Mapping):
        user_id = str(user_section.get("id") or "")
    message_section = payload.get("message")
    metadata: Mapping[str, Any] | None = None
    if isinstance(message_section, Mapping):
        candidate = message_section.get("metadata")
        if isinstance(candidate, Mapping):
            metadata = candidate
    order_id = 0
    if metadata is not None:
        raw_order = metadata.get("order_id")
        if isinstance(raw_order, int):
            order_id = raw_order
        elif isinstance(raw_order, str):
            try:
                order_id = int(raw_order)
            except ValueError:
                order_id = 0
    token_raw = metadata.get("token") if metadata else None
    token = token_raw if isinstance(token_raw, str) else None
    try:
        response = handler(action_id, user=user_id, order_id=order_id, token=token)
        message = str(response.get("text") or "ok")
    except Exception as exc:  # pragma: no cover - defensive
        log_event("slack", "handler", f"{action_id} error", level="ERROR", error=str(exc))
        respond("error")
        return
    respond(message)


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

    def _run_socket_mode(self) -> None:  # pragma: no cover - requires slack_bolt/slack_sdk
        # Hard dependency only when enabled.
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except Exception as exc:
            log_event(
                "slack",
                "socket_mode",
                "slack_bolt unavailable, using simulation",
                level="WARN",
                error=str(exc),
            )
            self.out.simulation = True
            self._run_simulation()
            return

        # Tokens from settings
        bot_token = self.settings.slack_bot_token
        app_token = self.settings.slack_app_token
        if not bot_token or not app_token:
            log_event(
                "slack",
                "socket_mode",
                "missing tokens, switching to simulation",
                level="WARN",
            )
            self.out.simulation = True
            self._run_simulation()
            return

        app = App(token=bot_token)

        @app.command("/cx")  # type: ignore[misc]
        def _cx_command(ack: Any, body: Any, respond: Any) -> None:
            process_slash_command_request(body, ack, respond)

        @app.action({"action_id": "confirm"})  # type: ignore[misc]
        def _confirm_action(ack: Any, body: Any, respond: Any) -> None:
            process_action_request("confirm", body, ack, respond)

        @app.action({"action_id": "reject"})  # type: ignore[misc]
        def _reject_action(ack: Any, body: Any, respond: Any) -> None:
            process_action_request("reject", body, ack, respond)

        log_event("slack", "startup", "slack socket-mode starting")
        SocketModeHandler(app, app_token).start()

    def run(self) -> None:
        ensure_runtime_dirs()
        warn_on_local_env("slack")
        _install_signal_handlers()
        mode = "sim" if self.out.simulation else "real"
        log_event("slack", "start", "slack service starting", mode=mode)
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
