"""Slack service providing simulation and socket-mode integrations."""

from __future__ import annotations

import base64
import json
import signal
import sys
import time
from threading import Event, Thread
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
import socket
import traceback

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.web import WebClient
from slack_sdk.web.slack_response import SlackResponse

from centrix.core.approvals import confirm as approve_order
from centrix.core.approvals import reject as reject_order
from centrix.core.logging import ensure_runtime_dirs, log_event, warn_on_local_env
from centrix.core.rbac import allow, role_of
from centrix.ipc import epoch_ms
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


def _dashboard_base_url() -> str:
    settings = get_settings()
    host = settings.dashboard_host or "127.0.0.1"
    port = settings.dashboard_port
    return f"http://{host}:{port}"


def _encode_basic_auth(user_id: str, role: str) -> str:
    credentials = f"{user_id}:{role}".encode("utf-8")
    token = base64.b64encode(credentials).decode("ascii")
    return f"Basic {token}"


def _dashboard_request(
    method: str,
    path: str,
    *,
    user_id: str,
    role: str,
    payload: Mapping[str, Any] | None = None,
    connect_timeout: float = 2.5,
    read_timeout: float = 5.0,
) -> tuple[bool, dict[str, Any] | None, str, int]:
    """Call dashboard API using shared Basic Auth credentials."""

    url = f"{_dashboard_base_url()}{path}"
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid dashboard url: {url}")

    headers = {"Authorization": _encode_basic_auth(user_id, role)}
    body_data: str | None = None
    if payload is not None:
        body_data = json.dumps(dict(payload))
        headers["Content-Type"] = "application/json"

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_cls = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_cls(parsed.hostname, port, timeout=connect_timeout)

    path_with_query = parsed.path or "/"
    if parsed.query:
        path_with_query = f"{path_with_query}?{parsed.query}"

    try:
        connection.request(method, path_with_query, body=body_data, headers=headers)
    except (socket.timeout, OSError, HTTPException) as exc:
        connection.close()
        reason = "timeout" if isinstance(exc, socket.timeout) else str(exc)
        return False, None, reason, 504 if isinstance(exc, socket.timeout) else 503

    try:
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        response = connection.getresponse()
        status = response.status
        resp_body = response.read()
    except socket.timeout:
        connection.close()
        return False, None, "timeout", 504
    except (HTTPException, OSError) as exc:
        connection.close()
        return False, None, str(exc), 502
    finally:
        connection.close()

    if not resp_body:
        payload_obj: dict[str, Any] = {}
    else:
        try:
            parsed_body = json.loads(resp_body.decode("utf-8"))
            payload_obj = parsed_body if isinstance(parsed_body, dict) else {}
        except json.JSONDecodeError:
            payload_obj = {}

    if status >= 400:
        reason = payload_obj.get("detail") if isinstance(payload_obj, dict) else ""
        if not reason:
            reason = getattr(response, "reason", "") or f"http {status}"
        return False, payload_obj, str(reason), status
    return True, payload_obj, "", status


def _status_api_call(
    user_id: str, role: str
) -> tuple[bool, dict[str, Any] | None, str, int]:
    return _dashboard_request("GET", "/api/status", user_id=user_id, role=role)


def _control_api_call(
    action: str,
    *,
    user_id: str,
    role: str,
    extra: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None, str, int]:
    payload = {"action": action}
    if extra:
        payload.update(extra)
    return _dashboard_request(
        "POST",
        "/api/control",
        user_id=user_id,
        role=role,
        payload=payload,
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_user_ref(user: str | None) -> str:
    if user and user.startswith("U"):
        return f"<@{user}>"
    return user or "system"


def _format_status_blocks(snapshot: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    mode = str(snapshot.get("mode") or "?")
    paused = "yes" if bool(snapshot.get("paused")) else "no"
    heartbeat = str(snapshot.get("heartbeat") or snapshot.get("ts") or "-")
    connectivity = snapshot.get("connectivity")
    if isinstance(connectivity, Mapping):
        connectivity_items = ", ".join(f"{k}:{v}" for k, v in connectivity.items())
        slack_status = connectivity.get("slack", "unknown")
    else:
        connectivity_items = "unavailable"
        slack_status = "unknown"
    risk = snapshot.get("risk") if isinstance(snapshot.get("risk"), Mapping) else {}
    pnl_day = _safe_float(getattr(risk, "get", lambda _: 0.0)("pnl_day"), 0.0)
    pnl_open = _safe_float(getattr(risk, "get", lambda _: 0.0)("pnl_open"), 0.0)
    margin_used = _safe_float(getattr(risk, "get", lambda _: 0.0)("margin_used_pct"), 0.0)
    text_fallback = (
        f"mode={mode} paused={paused} slack={slack_status} pnl_day={pnl_day:.2f} margin={margin_used:.1f}%"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Centrix System Snapshot", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Mode:* `{mode}`   •   *Paused:* `{paused}`\n*Heartbeat:* `{heartbeat}`",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Connectivity:* {connectivity_items}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*PnL Day*\n{pnl_day:.2f}"},
                {"type": "mrkdwn", "text": f"*PnL Open*\n{pnl_open:.2f}"},
                {"type": "mrkdwn", "text": f"*Margin Used*\n{margin_used:.1f}%"},
            ],
        },
    ]
    last_action = snapshot.get("last_action")
    if isinstance(last_action, Mapping) and last_action.get("action"):
        user_ref = _format_user_ref(str(last_action.get("user") or ""))
        action_text = str(last_action.get("action"))
        ts = str(last_action.get("ts") or "-")
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Last Action:* `{action_text}` by {user_ref} · {ts}",
                    }
                ],
            }
        )
    return text_fallback, blocks


def _control_update_message(action: str, user_id: str, snapshot: Mapping[str, Any]) -> str:
    mode = str(snapshot.get("mode") or "?")
    paused = "paused" if bool(snapshot.get("paused")) else "active"
    slack_status = "-"
    connectivity = snapshot.get("connectivity")
    if isinstance(connectivity, Mapping):
        slack_status = str(connectivity.get("slack", "unknown"))
    user_ref = _format_user_ref(user_id)
    return (
        f"{action.title()} requested by {user_ref} → mode={mode}, state={paused}, slack={slack_status}"
    )


def _order_announcement(
    user_id: str, order_info: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    order_id = order_info.get("id")
    symbol = order_info.get("symbol", "-")
    qty = order_info.get("qty", "-")
    px = order_info.get("px", "-")
    token = order_info.get("token")
    user_ref = _format_user_ref(user_id)
    text = (
        f"Order #{order_id} {symbol} qty={qty} px={px} initiated by {user_ref}. "
        f"Token: {token or '-'}"
    )
    metadata = {
        "order_id": order_id,
        "token": token,
        "type": "order.submit",
    }
    return text, metadata


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
        try:
            response = self._client.chat_postMessage(
                channel=channel,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
                metadata=metadata,
            )
        except (SlackApiError, SlackClientError) as exc:
            reason = None
            if getattr(exc, "response", None) is not None:
                data = getattr(exc.response, "data", None)
                if isinstance(data, dict):
                    reason = data.get("error") or data.get("detail")
            if not reason:
                reason = str(exc)
            log_event(
                "slack",
                "post.error",
                "failed to post slack message",
                level="ERROR",
                channel=channel,
                reason=reason,
            )
            return {"ok": False, "channel": channel, "error": reason}
        except Exception as exc:  # pragma: no cover - defensive
            reason = str(exc)
            log_event(
                "slack",
                "post.error",
                "failed to post slack message",
                level="ERROR",
                channel=channel,
                reason=reason,
            )
            return {"ok": False, "channel": channel, "error": reason}

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


def _control_notification_text(payload: Mapping[str, Any]) -> str:
    action = str(payload.get("action") or "?")
    status = str(payload.get("status") or "unknown").upper()
    ts = str(payload.get("ts") or _now_iso())
    user = payload.get("user")
    role = payload.get("role")
    parts = [f"Control {action}", f"status={status}", f"ts={ts}"]
    if user:
        parts.append(f"user={_format_user_ref(str(user))}")
    if role:
        parts.append(f"role={str(role)}")
    return " ".join(parts)


def dispatch_notifications(
    out: SlackOut,
    bus: Bus,
    last_event_id: int | None = None,
) -> int:
    """Deliver pending slack.notify events to Slack control channel."""

    current_id = last_event_id or 0
    events = bus.tail_events(limit=100, topic="slack.notify")
    new_events = [evt for evt in events if evt["id"] > current_id]
    if not new_events:
        return current_id
    for event in new_events:
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        if data.get("type") == "control-action":
            text = _control_notification_text(data)
            out.post_message(channel_for("control"), text, metadata={"type": "control"})
    current_id = max(evt["id"] for evt in new_events)
    return current_id


def run_selftest_cycle(out: SlackOut, bus: Bus) -> dict[str, Any]:
    """Execute the Slack selftest, post summary, and update bus detail."""

    result = slack_selftest()
    run_at = result.get("run_at") or _now_iso()
    status = str(result.get("status") or ("PASS" if result.get("overall_ok") else "FAIL"))
    healthy = bool(result.get("overall_ok"))
    detail = f"{'up' if healthy else 'down'} selftest {status} {run_at}"
    bus.set_service_detail("slack", detail)
    bus.record_heartbeat("slack", epoch_ms())

    latest_report = Path("runtime/reports/slack_selftest.json")
    latest_report.parent.mkdir(parents=True, exist_ok=True)
    latest_report.write_text(json.dumps(result, indent=2), encoding="utf-8")

    summary = f"Slack selftest {status} ({'OK' if healthy else 'FAIL'}) at {run_at}"
    out.post_message(channel_for("logs"), summary, metadata={"type": "selftest"})
    return result


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


def _help_text() -> str:
    return (
        "Centrix commands:\n"
        "• /cx status\n"
        "• /cx pause | /cx resume\n"
        "• /cx mode [mock|real]\n"
        "• /cx restart <service>\n"
        "• /cx order <SYMBOL> <QTY> <PX>\n"
        "• /cx help"
    )


def _ephemeral_payload(
    text: str, *, blocks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"response_type": "ephemeral", "text": text}
    if blocks:
        payload["blocks"] = blocks
    return payload


def _dashboard_error_message(reason: str, status: int) -> str:
    if reason == "timeout" or status == 504:
        return "dashboard timeout"
    if not reason:
        return f"dashboard error ({status})"
    return f"dashboard error ({status}): {reason}"


def handle_slash_command(
    *,
    user_id: str,
    channel: str,
    text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Handle `/cx` style slash commands."""

    tokens = [segment for segment in text.strip().split() if segment]
    if not tokens:
        return _ephemeral_payload(_help_text()), {"ok": True, "action": "help", "http_status": 200}

    command = tokens[0].lower()
    if command in {"help", "?"}:
        return _ephemeral_payload(_help_text()), {"ok": True, "action": "help", "http_status": 200}

    role = role_of(user_id)

    def _role_denied(action_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        message = f"access denied (role={role})"
        return _ephemeral_payload(message), {
            "ok": False,
            "action": action_name,
            "http_status": 403,
            "error": message,
        }

    if command == "status":
        success, snapshot, reason, status_code = _status_api_call(user_id, role)
        if not success or not isinstance(snapshot, dict):
            message = _dashboard_error_message(reason, status_code)
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "status",
                "http_status": status_code,
                "error": message,
            }
        text_body, blocks = _format_status_blocks(snapshot)
        return _ephemeral_payload(text_body, blocks=blocks), {
            "ok": True,
            "action": "status",
            "http_status": status_code,
        }

    if command in {"pause", "resume", "mode", "restart", "order"}:
        if not allow(command if command != "order" else "order", role):
            return _role_denied(command)

    if command == "pause":
        success, snapshot, reason, status_code = _control_api_call(
            "pause", user_id=user_id, role=role
        )
        if not success or not isinstance(snapshot, dict):
            message = _dashboard_error_message(reason, status_code)
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "pause",
                "http_status": status_code,
                "error": message,
            }
        announcement = _control_update_message("pause", user_id, snapshot)
        get_slack_out().post_message(channel_for("control"), announcement)
        text_body, blocks = _format_status_blocks(snapshot)
        return _ephemeral_payload(text_body, blocks=blocks), {
            "ok": True,
            "action": "pause",
            "http_status": status_code,
        }

    if command == "resume":
        success, snapshot, reason, status_code = _control_api_call(
            "resume", user_id=user_id, role=role
        )
        if not success or not isinstance(snapshot, dict):
            message = _dashboard_error_message(reason, status_code)
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "resume",
                "http_status": status_code,
                "error": message,
            }
        announcement = _control_update_message("resume", user_id, snapshot)
        get_slack_out().post_message(channel_for("control"), announcement)
        text_body, blocks = _format_status_blocks(snapshot)
        return _ephemeral_payload(text_body, blocks=blocks), {
            "ok": True,
            "action": "resume",
            "http_status": status_code,
        }

    if command == "mode":
        target = tokens[1].lower() if len(tokens) > 1 else None
        if target and target not in {"mock", "real"}:
            message = "usage: /cx mode [mock|real]"
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "mode",
                "http_status": 400,
                "error": message,
            }
        payload: dict[str, Any] = {}
        if target:
            payload["value"] = target
        success, snapshot, reason, status_code = _control_api_call(
            "mode", user_id=user_id, role=role, extra=payload
        )
        if not success or not isinstance(snapshot, dict):
            message = _dashboard_error_message(reason, status_code)
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "mode",
                "http_status": status_code,
                "error": message,
            }
        announcement = _control_update_message("mode", user_id, snapshot)
        get_slack_out().post_message(channel_for("control"), announcement)
        text_body, blocks = _format_status_blocks(snapshot)
        return _ephemeral_payload(text_body, blocks=blocks), {
            "ok": True,
            "action": "mode",
            "http_status": status_code,
        }

    if command == "restart":
        if len(tokens) < 2:
            message = "usage: /cx restart <service>"
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "restart",
                "http_status": 400,
                "error": message,
            }
        targets = tokens[1:]
        payload: dict[str, Any] = {"service": targets if len(targets) > 1 else targets[0]}
        success, snapshot, reason, status_code = _control_api_call(
            "restart", user_id=user_id, role=role, extra=payload
        )
        if not success or not isinstance(snapshot, dict):
            message = _dashboard_error_message(reason, status_code)
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "restart",
                "http_status": status_code,
                "error": message,
            }
        announcement = _control_update_message("restart", user_id, snapshot)
        get_slack_out().post_message(channel_for("control"), announcement)
        text_body, blocks = _format_status_blocks(snapshot)
        return _ephemeral_payload(text_body, blocks=blocks), {
            "ok": True,
            "action": "restart",
            "http_status": status_code,
        }

    if command == "order":
        if len(tokens) < 4:
            message = "usage: /cx order SYMBOL QTY PX"
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "order",
                "http_status": 400,
                "error": message,
            }
        symbol = tokens[1]
        try:
            qty = int(tokens[2])
            px = float(tokens[3])
        except ValueError:
            message = "quantity must be int, price float"
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "order",
                "http_status": 400,
                "error": message,
            }
        payload = {"symbol": symbol, "qty": qty, "px": px}
        success, snapshot, reason, status_code = _control_api_call(
            "order", user_id=user_id, role=role, extra=payload
        )
        if not success or not isinstance(snapshot, dict):
            message = _dashboard_error_message(reason, status_code)
            return _ephemeral_payload(message), {
                "ok": False,
                "action": "order",
                "http_status": status_code,
                "error": message,
            }
        last_action = snapshot.get("last_action")
        order_info: Mapping[str, Any] | None = None
        if isinstance(last_action, Mapping):
            details = last_action.get("details")
            if isinstance(details, Mapping):
                candidate = details.get("order")
                if isinstance(candidate, Mapping):
                    order_info = candidate
        if order_info:
            text_out, metadata = _order_announcement(user_id, order_info)
            get_slack_out().post_message(channel_for("orders"), text_out, metadata=metadata)
        announcement = _control_update_message("order", user_id, snapshot)
        get_slack_out().post_message(channel_for("control"), announcement)
        text_body, blocks = _format_status_blocks(snapshot)
        return _ephemeral_payload(text_body, blocks=blocks), {
            "ok": True,
            "action": "order",
            "http_status": status_code,
        }

    return _ephemeral_payload(_help_text()), {
        "ok": False,
        "action": command,
        "http_status": 400,
        "error": "unknown command",
    }


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
    ack: Callable[[dict[str, Any]], Any],
    respond: Callable[[dict[str, Any]], Any],
    *,
    handler: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = handle_slash_command,
) -> None:
    """Wrap slash command handling ensuring immediate acknowledgement and logging."""

    payload = dict(body) if isinstance(body, Mapping) else {}
    user_id = str(payload.get("user_id") or "")
    channel_id = str(payload.get("channel_id") or payload.get("channel", ""))
    text_raw = payload.get("text")
    text = str(text_raw) if text_raw is not None else ""
    tokens = [segment for segment in text.strip().split() if segment]
    action_hint = tokens[0].lower() if tokens else "help"

    start = time.monotonic()
    log_event(
        "slack",
        "command.start",
        "slash command received",
        user=user_id,
        channel=channel_id,
        text=text,
        action=action_hint,
    )

    ack(_ephemeral_payload("processing..."))

    try:
        response_payload, meta = handler(user_id=user_id, channel=channel_id, text=text)
    except Exception as exc:  # pragma: no cover - defensive
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(
            "slack",
            "command.error",
            "slash command failed",
            level="ERROR",
            user=user_id,
            channel=channel_id,
            text=text,
            action=action_hint,
            duration_ms=duration_ms,
            error=str(exc),
            stack=traceback.format_exc(),
        )
        short_reason = str(exc).splitlines()[0] if str(exc) else "unexpected error"
        respond(_ephemeral_payload(f"command failed: {short_reason}"))
        return

    respond(response_payload)

    duration_ms = int((time.monotonic() - start) * 1000)
    action_name = meta.get("action") or action_hint
    http_status = meta.get("http_status")
    if meta.get("ok", True):
        log_event(
            "slack",
            "command.ok",
            "slash command processed",
            user=user_id,
            channel=channel_id,
            text=text,
            action=action_name,
            duration_ms=duration_ms,
            http_status=http_status,
        )
    else:
        log_event(
            "slack",
            "command.error",
            "slash command completed with error",
            level="ERROR",
            user=user_id,
            channel=channel_id,
            text=text,
            action=action_name,
            duration_ms=duration_ms,
            http_status=http_status,
            error=meta.get("error"),
        )


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
        self.bus = Bus(self.settings.ipc_db)
        self._stop_event = Event()
        self._last_notify_id = 0
        existing = self.bus.tail_events(limit=1, topic="slack.notify")
        if existing:
            self._last_notify_id = existing[-1]["id"]

    def _background_tick(self) -> None:
        self._last_notify_id = dispatch_notifications(self.out, self.bus, self._last_notify_id)

    def _background_loop(self) -> None:
        run_selftest_cycle(self.out, self.bus)
        next_selftest = time.monotonic() + 60.0
        next_heartbeat = time.monotonic()
        while not self._stop_event.is_set():
            self._background_tick()
            now = time.monotonic()
            if now >= next_selftest:
                run_selftest_cycle(self.out, self.bus)
                next_selftest = now + 60.0
            if now >= next_heartbeat:
                self.bus.record_heartbeat("slack", epoch_ms())
                log_event(
                    "slack",
                    "heartbeat",
                    "slack service alive",
                    mode="sim" if self.out.simulation else "real",
                )
                next_heartbeat = now + 15.0
            time.sleep(2.0)

    def _run_simulation(self) -> None:
        log_event("slack", "startup", "slack service started (simulation)", mode="sim")
        try:
            self._background_loop()
        finally:
            self._stop_event.set()

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
        background = Thread(target=self._background_loop, daemon=True)
        background.start()
        try:
            SocketModeHandler(app, app_token).start()
        finally:
            self._stop_event.set()
            background.join(timeout=2.0)

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
