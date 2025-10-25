"""Slack Socket Mode service integration."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError, SlackClientError

from ..bus import enqueue_command, init_db, touch_service
from ..utils.env import get_env_str, get_role_map, mask

log = logging.getLogger("centrix.slack")

_client: Any | None = None
_control_channel: str | None = None
_simulation: bool = False
_role_map: dict[str, str] = {}

_LOCK_PATH = Path("runtime/locks/slack.lock")
_MAX_POST_ATTEMPTS = 3
_RETRYABLE_ERRORS = {"ratelimited", "rate_limited", "internal_error"}
_TRUTHY_VALUES = {"1", "true", "TRUE", "yes", "on", "ON"}
_HEARTBEAT_INTERVAL = 3.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


class _LockFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing = self.path.read_text().strip()
            except OSError:
                existing = ""
            pid = int(existing) if existing.isdigit() else None
            if pid and _pid_alive(pid):
                log.error("Slack service already running with pid=%s", pid)
                raise SystemExit(1)
            try:
                self.path.unlink()
            except OSError:
                log.warning("Unable to remove stale slack lockfile")
        try:
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            log.error("Failed to write slack lockfile: %s", exc)
            raise
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Failed to remove slack lockfile: %s", exc)
        finally:
            self.acquired = False


def _ensure_client() -> Any:
    if _client is None:
        raise RuntimeError("Slack app not initialised; call build_app() first")
    return _client


def _ensure_control_channel() -> str:
    if _control_channel is None:
        raise RuntimeError("Slack control channel not configured")
    return _control_channel


def build_app(env: Mapping[str, str] | None = None) -> App:
    """Create the Bolt App instance and register handlers."""
    source = env if env is not None else os.environ
    init_db()
    bot_token = get_env_str("SLACK_BOT_TOKEN", required=True, env=source)
    if bot_token is None:
        raise RuntimeError("missing env: SLACK_BOT_TOKEN")
    app = App(token=bot_token, token_verification_enabled=False, logger=log)

    global _client, _control_channel, _simulation, _role_map
    _client = app.client
    _control_channel = get_env_str("SLACK_CHANNEL_CONTROL", required=True, env=source)
    sim_value = source.get("SLACK_SIMULATION") if isinstance(source, Mapping) else os.getenv("SLACK_SIMULATION")
    _simulation = bool(sim_value) and str(sim_value) in _TRUTHY_VALUES
    _role_map = get_role_map()

    log.info(
        "Slack app configured (bot_token=%s channel=%s simulation=%s)",
        mask(bot_token, show=4),
        _control_channel,
        _simulation,
    )

    @app.event("app_mention")
    def on_mention(body: dict[str, Any], say: Callable[..., Any]) -> None:
        user = body.get("event", {}).get("user", "?")
        _say_threaded(say, "ack mention", body)
        log.info("Handled app_mention from %s", user)

    @app.action("approve_btn")
    def on_approve(
        ack: Callable[[], None],
        body: dict[str, Any],
        say: Callable[..., Any],
        logger: logging.Logger,
    ) -> None:
        ack()
        action = (body.get("actions") or [{}])[0]
        order_id = action.get("value") or "?"
        user_id = body.get("user", {}).get("id", "?")
        role = _role_map.get(user_id, "viewer")

        if role != "admin":
            _say_threaded(say, "forbidden", body)
            logger.warning("approve_btn forbidden for %s role=%s", user_id, role)
            return

        try:
            cmd_id = enqueue_command(
                "APPROVE",
                {"id": order_id},
                requested_by=user_id,
                role=role,
                ttl_sec=300,
            )
            _say_threaded(say, f"queued {cmd_id}", body)
            logger.info("approve_btn handled for %s by %s (cmd=%s)", order_id, user_id, cmd_id)
        except Exception as exc:
            _say_threaded(say, f"queue failed: {exc}", body)
            logger.exception("Failed to enqueue approve command")

    def _slash_command(
        ack: Callable[[], None],
        body: dict[str, Any],
        respond: Callable[..., Any],
        logger: logging.Logger,
    ) -> None:
        ack()
        text = (body.get("text") or "").strip()
        user_id = body.get("user_id", "?")
        role = _role_map.get(user_id, "viewer")
        try:
            response = _handle_command(text, user_id, role, body)
        except Exception as exc:
            logger.exception("Command processing failed")
            response = f"centrix: error {exc}"
        respond(response)
        logger.info("Handled slash command '%s' by %s role=%s", text or "<empty>", user_id, role)

    @app.command("/centrix")
    def on_centrix(
        ack: Callable[[], None],
        body: dict[str, Any],
        respond: Callable[..., Any],
        logger: logging.Logger,
    ) -> None:
        _slash_command(ack, body, respond, logger)

    @app.command("/cx")
    def on_cx(
        ack: Callable[[], None],
        body: dict[str, Any],
        respond: Callable[..., Any],
        logger: logging.Logger,
    ) -> None:
        _slash_command(ack, body, respond, logger)

    return app


def _say_threaded(say: Callable[..., Any], text: str, body: Mapping[str, Any]) -> None:
    container = body.get("container") if isinstance(body, Mapping) else None
    thread_ts = None
    if isinstance(container, Mapping):
        thread_ts = container.get("thread_ts") or container.get("message_ts")
    kwargs: dict[str, Any] = {"text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    say(**kwargs)


def _handle_command(text: str, user_id: str, role: str, _body: Mapping[str, Any]) -> str:
    if not text:
        return "Usage: /centrix <ping|approve <id>|deny <id>|health>"

    parts = text.split()
    command = parts[0].lower()

    if command == "ping":
        return "centrix: pong"

    if command == "health":
        result = healthcheck()
        return (
            "centrix: health "
            f"auth_ok={result.get('auth_ok')} "
            f"post_ok={result.get('post_ok')} "
            f"channel={result.get('channel')}"
        )

    if command in {"approve", "deny"}:
        if len(parts) < 2:
            return f"centrix: missing identifier for {command}"
        ident = parts[1]
        if role != "admin":
            return "centrix: forbidden"
        cmd_type = "APPROVE" if command == "approve" else "DENY"
        cmd_id = enqueue_command(
            cmd_type,
            {"id": ident},
            requested_by=user_id,
            role=role,
            ttl_sec=300,
        )
        return f"centrix: queued {cmd_id}"

    return f"centrix: unknown command '{command}'"


def post_control(text: str, blocks: list[dict[str, Any]] | None = None) -> str:
    """Post a message to the control channel with retry/backoff."""
    control = _ensure_control_channel()
    client = _ensure_client()

    if _simulation:
        log.info("[SIM] control post -> %s", text)
        return "sim"

    delay = 1.0
    last_error: str | None = None
    for attempt in range(1, _MAX_POST_ATTEMPTS + 1):
        try:
            response = client.chat_postMessage(channel=control, text=text, blocks=blocks)
            ts = response.get("ts") or ""
            log.info("Posted control message ts=%s", ts)
            return ts
        except SlackApiError as exc:
            error_code = ""
            if exc.response is not None:
                error_code = exc.response.get("error", "") or ""
            last_error = error_code or str(exc)
            log.warning(
                "Slack post attempt %s failed (error=%s)", attempt, last_error, exc_info=exc
            )
            if error_code not in _RETRYABLE_ERRORS or attempt == _MAX_POST_ATTEMPTS:
                raise
            retry_after = 0.0
            if exc.response is not None:
                headers = getattr(exc.response, "headers", {}) or {}
                retry_header = headers.get("Retry-After")
                try:
                    retry_after = float(retry_header) if retry_header is not None else 0.0
                except (TypeError, ValueError):
                    retry_after = 0.0
            wait_for = max(delay, retry_after)
            time.sleep(wait_for)
            delay *= 2
    raise RuntimeError(f"Failed to post control message: {last_error}")


def healthcheck() -> dict[str, Any]:
    """Run auth and post probes returning aggregated information."""
    client = _ensure_client()
    control = _ensure_control_channel()

    result: dict[str, Any] = {
        "auth_ok": False,
        "post_ok": False,
        "channel": control,
        "bot_id": None,
        "user_id": None,
        "ts": None,
        "error": None,
    }

    try:
        auth = client.auth_test()
        result["bot_id"] = auth.get("bot_id")
        result["user_id"] = auth.get("user_id")
        result["auth_ok"] = bool(auth.get("ok", True))
    except Exception as exc:  # pragma: no cover - safety net
        log.exception("auth_test failed")
        result["error"] = f"auth_test: {exc}"
        return result

    try:
        ts = post_control("centrix healthcheck")
        result["ts"] = ts
        result["post_ok"] = True
    except Exception as exc:
        log.exception("control post during healthcheck failed")
        result["error"] = f"post_control: {exc}"

    return result


def run_socket_mode(app_token: str, app: App) -> None:
    """Start the socket mode handler with clean shutdown semantics."""
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN is required")

    lock = _LockFile(_LOCK_PATH)
    lock.acquire()

    handler = SocketModeHandler(app, app_token)
    stop_event = threading.Event()
    closed = threading.Event()
    session_id = f"{os.getpid()}-{int(time.time())}"
    touch_service("slack", "up", {"session": session_id})

    def _heartbeat_loop() -> None:
        try:
            while not stop_event.is_set():
                touch_service("slack", "up", {"session": session_id})
                if stop_event.wait(_HEARTBEAT_INTERVAL):
                    break
        except Exception:  # pragma: no cover - defensive
            log.exception("Slack heartbeat loop failed")

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        name="centrix-slack-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    def _close_handler() -> None:
        if closed.is_set():
            return
        closed.set()
        try:
            handler.close()
        except SlackClientError:
            log.warning("Socket handler close raised SlackClientError", exc_info=True)

    def _shutdown(signum: int, _frame: Any) -> None:
        first = not stop_event.is_set()
        stop_event.set()
        if first:
            log.info("Slack socket mode shutdown requested (signal=%s)", signum)
        _close_handler()

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    def _run() -> None:
        try:
            handler.start()
        except Exception:
            log.exception("Slack socket handler crashed")
            stop_event.set()

    thread = threading.Thread(target=_run, name="centrix-slack-socket", daemon=True)
    thread.start()
    log.info("Socket mode started (app_token=%s)", mask(app_token, show=4))

    try:
        while thread.is_alive() and not stop_event.is_set():
            thread.join(timeout=0.2)
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)
    finally:
        stop_event.set()
        _close_handler()
        thread.join(timeout=2.0)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        stop_event.set()
        heartbeat_thread.join(timeout=2.0)
        touch_service("slack", "down", {"session": session_id})
        alive = thread.is_alive()
        lock.release()
        if alive:
            log.warning("Socket thread still alive after shutdown; forcing exit")
            os._exit(0)
        log.info("Slack socket mode stopped cleanly")
