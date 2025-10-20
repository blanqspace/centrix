"""FastAPI dashboard providing control API, WebSocket feed, and HTML UI."""

from __future__ import annotations

import asyncio
import platform
import secrets
import sys
from datetime import datetime
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
from centrix.core.logging import log_event
from centrix.core.metrics import METRICS, snapshot_kpis
from centrix.core.orders import add_order, list_orders
from centrix.ipc import read_state, write_state
from centrix.ipc.bus import Bus
from centrix.settings import get_settings

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

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Centrix Dashboard</title>
    <style>
      body {
        font-family: sans-serif;
        margin: 0;
        padding: 0;
        background: #101820;
        color: #f0f3f7;
      }
      header {
        background: #17202b;
        padding: 1rem;
        display: flex;
        gap: 1rem;
        align-items: center;
      }
      main {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1rem;
        padding: 1rem;
      }
      section {
        background: #1c2733;
        border-radius: 8px;
        padding: 1rem;
        min-height: 200px;
        overflow: auto;
      }
      h2 {
        margin-top: 0;
        font-size: 1rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      button, select, input {
        background: #243447;
        color: #f0f3f7;
        border: 1px solid #2f4052;
        padding: 0.4rem 0.6rem;
        border-radius: 4px;
        margin-right: 0.4rem;
      }
      button:hover { background: #2f4052; cursor: pointer; }
      small { color: #8a9ba8; display: block; margin-top: 0.4rem; }
      table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
      th, td { border-bottom: 1px solid #243447; padding: 0.3rem; text-align: left; }
      pre { white-space: pre-wrap; word-break: break-word; }
    </style>
  </head>
  <body>
    <header>
      <strong>Centrix Control</strong>
      <button id="btn-pause">Pause</button>
      <button id="btn-resume">Resume</button>
      <button id="btn-mode">Toggle Mode</button>
      <select id="svc-select">
        <option value="tui">Restart TUI</option>
        <option value="dashboard">Restart Dashboard</option>
        <option value="worker">Restart Worker</option>
      </select>
      <button id="btn-restart">Restart</button>
      <button id="btn-order">Test Order</button>
      <input id="token-input" placeholder="Token (optional)" aria-label="Dashboard token">
      <small id="status-indicator">Connecting…</small>
    </header>
    <main>
      <section id="state-panel">
        <h2>State</h2>
        <div id="state"></div>
      </section>
      <section id="clients-panel">
        <h2>Clients</h2>
        <div id="clients"></div>
      </section>
      <section id="orders-panel">
        <h2>Orders</h2>
        <div id="orders"></div>
      </section>
      <section id="events-panel">
        <h2>Events</h2>
        <div id="events"></div>
      </section>
    </main>
    <script>
      const indicator = document.getElementById('status-indicator');
      const tokenInput = document.getElementById('token-input');
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
      document
        .getElementById('btn-order')
        .addEventListener('click', () =>
          sendAction('test-order', { symbol: 'DEMO', qty: 1, px: 0 }),
        );
      document.getElementById('btn-restart').addEventListener('click', () => {
        const svc = document.getElementById('svc-select').value;
        sendAction('restart', { service: svc });
      });
      tokenInput.addEventListener('change', () => {
        currentToken = tokenInput.value.trim();
        openSocket();
        fetchStatus();
      });

      openSocket();
      fetchStatus();
    </script>
  </body>
</html>
"""


def _require_token(request: Request) -> None:
    token = settings.dashboard_auth_token
    if not token:
        return
    supplied = request.headers.get("X-Dashboard-Token")
    if supplied != token:
        raise HTTPException(status_code=401, detail="unauthorized")


def status_payload() -> dict[str, Any]:
    bus = Bus(settings.ipc_db)
    state = read_state()
    services = bus.get_services_status(SERVICE_NAMES)
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
    bus = Bus(settings.ipc_db)
    services = bus.get_services_status(SERVICE_NAMES)
    ok = all(info.get("running") for info in services.values())
    return {"ok": ok, "services": services}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    data = status_payload()
    return {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "kpi": data["kpi"],
        "alerts": data.get("alerts", {}),
        "services": data["services"],
        "build": BUILD_INFO,
    }


@app.get("/api/status")
async def api_status(_: None = Depends(_require_token)) -> JSONResponse:
    return JSONResponse(status_payload())


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
    token = settings.dashboard_auth_token
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
