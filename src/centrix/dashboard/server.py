"""FastAPI dashboard providing control API, WebSocket feed, and HTML UI."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse

from centrix import __version__
from centrix.cli import _start_service, _stop_service
from centrix.core.alerts import alert_counters
from centrix.core.logging import log_event, warn_on_local_env
from centrix.core.metrics import METRICS, snapshot_kpis
from centrix.core.orders import add_order, list_orders
from centrix.ipc import read_state, write_state
from centrix.ipc.bus import Bus
from centrix.settings import AppSettings, get_settings

settings = get_settings()

SERVICE_NAMES = ["tui", "dashboard", "worker", "slack"]
BUILD_INFO = {
    "version": __version__,
    "py": sys.version.split()[0],
    "platform": platform.platform(),
}
CLIENTS: dict[str, dict[str, Any]] = {}
WS_PUSH_INTERVAL = 2.0
EVENT_LIMIT = 25

app = FastAPI(title=settings.app_brand, version=__version__)
warn_on_local_env("dashboard")

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Centrix Dashboard</title>
    <style>
      :root {
        --bg: #f7f7f8;
        --card: #ffffff;
        --text: #0a0a0a;
        --muted: #555555;
        --border: #e5e7eb;
        --primary: #1d4ed8;
        --primary-hover: #1e40af;
        --header: #ffffff;
      }
      .dark {
        --bg: #101820;
        --card: #1c2733;
        --text: #f0f3f7;
        --muted: #a0aec0;
        --border: #243447;
        --primary: #3b82f6;
        --primary-hover: #2563eb;
        --header: #17202b;
      }
      * {
        box-sizing: border-box;
      }
      body {
        font-family: "Inter", "Segoe UI", system-ui, sans-serif;
        margin: 0;
        padding: 0;
        background: var(--bg);
        color: var(--text);
        font-size: 18px;
        line-height: 1.5;
        transition: background 0.3s ease, color 0.3s ease;
      }
      header {
        background: var(--header);
        padding: 1.2rem 1.5rem;
        display: flex;
        gap: 0.75rem;
        align-items: center;
        flex-wrap: wrap;
        border-bottom: 1px solid var(--border);
      }
      header strong {
        font-size: 1.15rem;
        margin-right: auto;
      }
      main {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 1.25rem;
        padding: 1.5rem;
      }
      section.card {
        background: var(--card);
        border-radius: 12px;
        padding: 1rem;
        min-height: 220px;
        border: 1px solid var(--border);
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05);
        overflow: auto;
        transition: background 0.3s ease, color 0.3s ease, border 0.3s ease;
      }
      section.card h3 {
        font-weight: 700;
        margin: 0 0 0.75rem;
        letter-spacing: 0.02em;
      }
      button {
        background: var(--primary);
        color: #ffffff;
        border: 1px solid transparent;
        padding: 0.5rem 0.9rem;
        border-radius: 8px;
        font-size: 0.95rem;
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        transition: background 0.2s ease, transform 0.1s ease, box-shadow 0.2s ease;
      }
      button:hover {
        background: var(--primary-hover);
        cursor: pointer;
      }
      button:active {
        transform: translateY(1px);
      }
      button:focus-visible {
        outline: 3px solid rgba(59, 130, 246, 0.45);
        outline-offset: 2px;
      }
      button.secondary {
        background: transparent;
        color: var(--text);
        border-color: var(--border);
      }
      button.secondary:hover {
        background: rgba(59, 130, 246, 0.12);
      }
      small {
        color: var(--muted);
        display: block;
        margin-top: 0.4rem;
        font-size: 0.85rem;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.92rem;
      }
      th,
      td {
        border-bottom: 1px solid var(--border);
        padding: 0.45rem 0;
        text-align: left;
      }
      pre {
        white-space: pre-wrap;
        word-break: break-word;
      }
    </style>
  </head>
  <body>
    <header>
      <strong>Centrix Control</strong>
      <button id="btn-pause" class="secondary">Pause</button>
      <button id="btn-resume" class="secondary">Fortsetzen</button>
      <button id="btn-mode">Modus wechseln</button>
      <button id="btn-restart-tui" class="secondary">TUI neu starten</button>
      <button id="btn-order">Test-Order</button>
      <button id="btn-dark-mode" class="secondary">Dark Mode</button>
      <input id="token-input" placeholder="Token (optional)" aria-label="Dashboard token">
      <small id="status-indicator">Connecting…</small>
    </header>
    <main>
      <section id="state-panel" class="card">
        <h3>SYSTEMSTATUS</h3>
        <div id="state"></div>
      </section>
      <section id="clients-panel" class="card">
        <h3>VERBINDUNGEN</h3>
        <div id="clients"></div>
      </section>
      <section id="orders-panel" class="card">
        <h3>AUFTRÄGE</h3>
        <div id="orders"></div>
      </section>
      <section id="events-panel" class="card">
        <h3>EREIGNISSE</h3>
        <div id="events"></div>
      </section>
    </main>
    <script>
      const indicator = document.getElementById('status-indicator');
      const tokenInput = document.getElementById('token-input');
      const darkModeBtn = document.getElementById('btn-dark-mode');
      let currentToken = '';
      let ws = null;
      let pollInterval = null;

      function headers() {
        const h = { 'Content-Type': 'application/json' };
        if (currentToken) {
          h['X-Dashboard-Token'] = currentToken;
        }
        return h;
      }

      function renderState(data) {
        const stateDiv = document.getElementById('state');
        const lines = [];
        if (data.state) {
          lines.push(`Mode: ${data.state.mode} (mock=${data.state.mode_mock})`);
          lines.push(`Paused: ${data.state.paused}`);
        }
        if (data.kpi) {
          lines.push(`Errors 1m: ${data.kpi.errors_1m ?? 0}`);
          const actions = data.kpi.counters?.['control.actions_total'] ?? 0;
          lines.push(`Control actions: ${actions}`);
        }
        if (data.services) {
          Object.entries(data.services).forEach(([name, info]) => {
            lines.push(`${name}: ${info.running ? 'running' : 'stopped'} pid=${info.pid ?? '-'}`);
          });
        }
        stateDiv.textContent = lines.join('\\n');
      }

      function renderClients(list) {
        const container = document.getElementById('clients');
        if (!list || list.length === 0) {
          container.textContent = 'No active clients';
          return;
        }
        const rows = list.map(c => `<div>${c.id} @ ${c.remote} (since ${c.connected_at})</div>`);
        container.innerHTML = rows.join('');
      }

      function renderOrders(list) {
        const container = document.getElementById('orders');
        if (!list || list.length === 0) {
          container.textContent = 'No orders';
          return;
        }
        const rows = list
          .slice(0, 10)
          .map(o => `<div>${o.ts} ${o.symbol} qty=${o.qty} px=${o.px} src=${o.source}</div>`);
        container.innerHTML = rows.join('');
      }

      let eventBuffer = [];
      function renderEvents(evts) {
        const container = document.getElementById('events');
        eventBuffer = evts.concat(eventBuffer).slice(0, 25);
        container.innerHTML = eventBuffer.map(evt => {
          const payload = JSON.stringify(evt.data ?? {}, null, 0);
          return `<div>${evt.id} ${evt.topic} [${evt.level}] ${payload}</div>`;
        }).join('');
      }

      function applyStatus(data) {
        renderState(data);
        renderClients(data.clients || []);
        renderOrders(data.orders || []);
        if (data.events) {
          renderEvents(data.events);
        }
      }

      function fetchStatus() {
        fetch('/api/status', { headers: headers() })
          .then(r => {
            if (r.status === 401) {
              indicator.textContent = 'Unauthorized';
              return r.json().then(j => { throw new Error(j.detail || 'Unauthorized'); });
            }
            return r.json();
          })
          .then(data => {
            indicator.textContent = 'Polling';
            applyStatus(data);
          })
          .catch(() => {});
      }

      function openSocket() {
        if (ws) {
          ws.close();
          ws = null;
        }
        if (pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
        let url = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
        if (currentToken) {
          url += `?token=${encodeURIComponent(currentToken)}`;
        }
        ws = new WebSocket(url);
        ws.addEventListener('open', () => {
          indicator.textContent = 'Live';
        });
        ws.addEventListener('message', event => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'status') {
              applyStatus(data.payload);
            } else if (data.type === 'events') {
              renderEvents(data.events || []);
            }
          } catch (err) {
            console.error('Invalid frame', err);
          }
        });
        ws.addEventListener('close', () => {
          indicator.textContent = 'Disconnected';
          pollInterval = setInterval(fetchStatus, 3000);
        });
        ws.addEventListener('error', () => {
          indicator.textContent = 'Error';
        });
      }

      function sendAction(action, body = {}) {
        const payload = { action, ...body };
        fetch('/api/control', {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify(payload),
        })
          .then(r => {
            if (r.status === 401) {
              indicator.textContent = 'Unauthorized';
            }
            return r.json();
          })
          .then(() => fetchStatus())
          .catch(err => console.error(err));
      }

      document.getElementById('btn-pause').addEventListener('click', () => sendAction('pause'));
      document.getElementById('btn-resume').addEventListener('click', () => sendAction('resume'));
      document.getElementById('btn-mode').addEventListener('click', () => sendAction('mode'));
      document.getElementById('btn-order').addEventListener('click', () =>
        sendAction('test-order', { symbol: 'DEMO', qty: 1, px: 0 }),
      );
      document.getElementById('btn-restart-tui').addEventListener('click', () => {
        sendAction('restart', { service: 'tui' });
      });
      tokenInput.addEventListener('change', () => {
        currentToken = tokenInput.value.trim();
        openSocket();
        fetchStatus();
      });

      openSocket();
      fetchStatus();

      darkModeBtn.addEventListener('click', () => {
        document.body.classList.toggle('dark');
        darkModeBtn.textContent = document.body.classList.contains('dark')
          ? 'Light Mode'
          : 'Dark Mode';
      });
    </script>
  </body>
</html>
"""


def _require_token(request: Request) -> None:
    token = os.environ.get("DASHBOARD_AUTH_TOKEN") or get_settings().dashboard_auth_token
    if not token:
        return
    supplied = request.headers.get("X-Dashboard-Token")
    if supplied != token:
        raise HTTPException(status_code=401, detail="unauthorized")


def _load_slack_selftest_detail() -> str | None:
    report_path = Path("runtime/reports/slack_selftest.json")
    if not report_path.exists():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "slack selftest report unreadable"

    status = payload.get("status") or ("PASS" if payload.get("overall_ok") else "FAIL")
    run_at = payload.get("run_at") or "-"

    auth_info = payload.get("auth") or {}
    if auth_info.get("ok") is True:
        auth_summary = "ok"
    elif auth_info.get("ok") is None:
        auth_summary = "skip"
    else:
        auth_summary = auth_info.get("code") or "fail"

    channels = payload.get("channels") or []
    total_channels = sum(1 for item in channels if item.get("channel"))
    ok_channels = sum(1 for item in channels if item.get("channel") and item.get("ok") is True)

    socket_info = payload.get("socket_mode") or {}
    if socket_info.get("ok") is True:
        socket_summary = "ok"
    elif socket_info.get("ok") is None:
        socket_summary = "skip"
    else:
        socket_summary = socket_info.get("code") or "fail"

    return (
        f"{status} {run_at} auth={auth_summary} "
        f"posts={ok_channels}/{total_channels} socket={socket_summary}"
    )


def status_payload() -> dict[str, Any]:
    bus = Bus(settings.ipc_db)
    state = read_state()
    services = bus.get_services_status(SERVICE_NAMES)
    slack_detail = _load_slack_selftest_detail()
    if slack_detail is not None:
        if "slack" in services and isinstance(services["slack"], dict):
            services["slack"]["detail"] = slack_detail
        else:
            services["slack"] = {"detail": slack_detail}
    kpi = snapshot_kpis()
    orders = list_orders()
    events = bus.tail_events(limit=EVENT_LIMIT)
    clients = list(CLIENTS.values())
    return {
        "state": state,
        "services": services,
        "kpi": kpi,
        "orders": orders,
        "events": events,
        "clients": clients,
        "build": BUILD_INFO,
        "alerts": alert_counters(),
    }


def _record_action(action: str, status: str = "ok", **fields: Any) -> None:
    METRICS.increment_counter("control.actions_total")
    log_event(
        "dashboard",
        f"control.{action}",
        "dashboard action executed",
        action=action,
        status=status,
        user="dashboard",
        **fields,
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(content=INDEX_HTML)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    services: dict[str, dict[str, Any]]
    try:
        bus = Bus(settings.ipc_db)
        services = bus.get_services_status(SERVICE_NAMES)
    except Exception as exc:  # pragma: no cover - defensive
        services = {"error": {"message": str(exc)}}
    return {
        "ok": True,
        "services": services,
        "ts": datetime.now(UTC).isoformat(),
    }


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    try:
        data = status_payload()
        return {
            "ok": True,
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "kpi": data["kpi"],
            "alerts": data.get("alerts", {}),
            "services": data["services"],
            "build": BUILD_INFO,
        }
    except Exception as exc:  # pragma: no cover - defensive
        log_event("dashboard", "metrics", "metrics error", level="ERROR", error=str(exc))
        return {"ok": False, "error": str(exc)}


@app.get("/api/status")
async def api_status(_: None = Depends(_require_token)) -> JSONResponse:
    try:
        payload = status_payload()
        payload["ok"] = True
        return JSONResponse(payload)
    except Exception as exc:  # pragma: no cover - defensive
        log_event("dashboard", "api.status", "status error", level="ERROR", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)})


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    settings_override = AppSettings()

    uvicorn.run(
        "centrix.dashboard.server:create_app",
        host=settings_override.dashboard_host,
        port=settings_override.dashboard_port,
        factory=True,
        log_level="info",
    )


def _toggle_mode(current: dict[str, Any], value: str | None) -> dict[str, Any]:
    target = value.lower() if value else None
    if target not in {None, "mock", "real"}:
        raise HTTPException(status_code=400, detail="invalid mode value")
    if target is None:
        target = "real" if current.get("mode") == "mock" else "mock"
    mode_mock = target == "mock"
    return write_state(mode=target, mode_mock=mode_mock)


def _restart_service(name: str) -> dict[str, Any]:
    if name not in SERVICE_NAMES:
        raise HTTPException(status_code=400, detail="unknown service")
    before = Bus(settings.ipc_db).get_services_status([name])[name]
    stopped = _stop_service(name)
    started = _start_service(name)
    after = Bus(settings.ipc_db).get_services_status([name])[name]
    return {
        "service": name,
        "stopped": stopped,
        "started": started,
        "pid_before": before.get("pid"),
        "pid_after": after.get("pid"),
    }


@app.post("/api/control")
async def api_control(request: Request, _: None = Depends(_require_token)) -> JSONResponse:
    payload = await request.json()
    return JSONResponse(_handle_control_action(payload))


def _handle_control_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="action required")

    state = read_state()
    result: dict[str, Any] = {"action": action}

    if action == "pause":
        state = write_state(paused=True)
        result["state"] = state
    elif action == "resume":
        state = write_state(paused=False)
        result["state"] = state
    elif action == "mode":
        state = _toggle_mode(state, payload.get("value"))
        result["state"] = state
    elif action == "restart":
        service = payload.get("service")
        if not service:
            raise HTTPException(status_code=400, detail="service required")
        result["restart"] = _restart_service(service)
    elif action == "test-order":
        order_payload = {
            "source": "dashboard",
            "symbol": payload.get("symbol", "DEMO"),
            "qty": payload.get("qty", 1),
            "px": payload.get("px", 0),
        }
        result["order"] = add_order(order_payload)
    else:
        raise HTTPException(status_code=400, detail="unknown action")

    _record_action(action, **{k: v for k, v in result.items() if k != "action"})
    return {"status": "ok", **result}


def _ws_authorized(websocket: WebSocket) -> bool:
    token = os.environ.get("DASHBOARD_AUTH_TOKEN") or get_settings().dashboard_auth_token
    if not token:
        return True
    query_token = websocket.query_params.get("token")
    header_token = websocket.headers.get("X-Dashboard-Token")
    return token in {query_token, header_token}


def _client_snapshot(websocket: WebSocket, client_id: str) -> dict[str, Any]:
    return {
        "id": client_id,
        "remote": getattr(websocket.client, "host", "?"),
        "connected_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def _events_since(last_id: int | None) -> list[dict[str, Any]]:
    bus = Bus(settings.ipc_db)
    events = bus.tail_events(limit=EVENT_LIMIT)
    filtered = [evt for evt in events if last_id is None or evt["id"] > last_id]
    return filtered


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client_id = secrets.token_hex(4)
    CLIENTS[client_id] = _client_snapshot(websocket, client_id)
    last_event_id: int | None = None
    try:
        while True:
            await websocket.send_json({"type": "status", "payload": status_payload()})
            events = _events_since(last_event_id)
            if events:
                last_event_id = events[-1]["id"]
                await websocket.send_json({"type": "events", "events": events})
            await asyncio.sleep(WS_PUSH_INTERVAL)
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.pop(client_id, None)
