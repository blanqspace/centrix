"""FastAPI dashboard providing control API, WebSocket feed, and HTML UI."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import sys
import time
from base64 import b64decode
from dataclasses import dataclass
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
from starlette.requests import ClientDisconnect

from centrix import __version__
from centrix.bus import get_services, touch_service
from centrix.cli import _parse_targets, _start_service, _stop_service
from centrix.core.alerts import alert_counters
from centrix.core.approvals import request_approval
from centrix.core.logging import log_event, warn_on_local_env
from centrix.core.metrics import METRICS, snapshot_kpis
from centrix.core.rbac import allow
from centrix.core.orders import add_order, list_orders
from centrix.ipc import read_state, write_state
from centrix.ipc.bus import Bus
from centrix.settings import AppSettings, get_settings

settings = get_settings()

SERVICE_NAMES = ["tui", "dashboard", "worker", "slack", "ibkr"]
BUILD_INFO = {
    "version": __version__,
    "py": sys.version.split()[0],
    "platform": platform.platform(),
}
CLIENTS: dict[str, dict[str, Any]] = {}
WS_PUSH_INTERVAL = 2.0
EVENT_LIMIT = 25
LAST_ACTION: dict[str, Any] | None = None
AUTH_ALLOWED_ROLES = {"observer", "operator", "admin"}
_HEARTBEAT_INTERVAL = 5.0
_HEARTBEAT_TASK: asyncio.Task | None = None
HEALTH_WINDOW = 10.0


@dataclass(slots=True)
class ControlIdentity:
    """Represents the caller interacting with control endpoints."""

    principal: str
    user: str | None
    role: str


class DashboardUnauthorized(Exception):
    """Raised when dashboard authentication fails."""

    def __init__(self, reason: str, user: str | None, has_token: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user = user
        self.has_token = has_token

app = FastAPI(title=settings.app_brand, version=__version__)
warn_on_local_env("dashboard")

@app.exception_handler(DashboardUnauthorized)
async def handle_dashboard_unauthorized(_: Request, exc: DashboardUnauthorized) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"ok": False, "error": "unauthorized", "reason": exc.reason},
    )


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
      .status-grid,
      .kpi-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 0.75rem;
        margin-bottom: 0.75rem;
      }
      .status-grid div,
      .kpi-grid div {
        background: rgba(59, 130, 246, 0.08);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 0.6rem;
      }
      .status-grid span,
      .kpi-grid span {
        display: block;
        color: var(--muted);
        font-size: 0.75rem;
        letter-spacing: 0.05em;
      }
      .status-grid strong,
      .kpi-grid strong {
        display: block;
        margin-top: 0.25rem;
        font-size: 1.1rem;
      }
      .connectivity-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin-bottom: 0.5rem;
      }
      .chip {
        padding: 0.2rem 0.6rem;
        border-radius: 999px;
        border: 1px solid var(--border);
        background: rgba(15, 118, 110, 0.08);
        font-size: 0.85rem;
      }
      .chip.up {
        color: #15803d;
      }
      .chip.down {
        color: #dc2626;
      }
      .chip.unknown {
        color: var(--muted);
      }
      #last-action {
        color: var(--muted);
        font-size: 0.85rem;
      }
      .clients-list div {
        padding: 0.35rem 0;
        border-bottom: 1px solid var(--border);
        font-size: 0.9rem;
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
        <div id="state" class="status-grid"></div>
        <div id="kpi" class="kpi-grid"></div>
        <div id="last-action"></div>
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
      const STORAGE_KEY = 'centrix.dashboard.token';
      let currentToken = '';
      let ws = null;
      let pollInterval = null;

      function readStoredToken() {
        try {
          return window.localStorage.getItem(STORAGE_KEY) || '';
        } catch (err) {
          console.warn('Local storage unavailable', err);
          return '';
        }
      }

      function persistToken(value) {
        try {
          if (value) {
            window.localStorage.setItem(STORAGE_KEY, value);
          } else {
            window.localStorage.removeItem(STORAGE_KEY);
          }
        } catch (err) {
          console.warn('Failed to persist token', err);
        }
      }

      currentToken = readStoredToken();
      if (currentToken) {
        tokenInput.value = currentToken;
      }

      function headers() {
        const h = { 'Content-Type': 'application/json' };
        if (currentToken) {
          h['X-Dashboard-Token'] = currentToken;
        }
        return h;
      }

      function formatNumber(value, digits = 2) {
        const num = Number(value);
        return Number.isFinite(num) ? num.toFixed(digits) : (0).toFixed(digits);
      }

      function formatPercent(value, digits = 1) {
        const num = Number(value);
        return Number.isFinite(num) ? num.toFixed(digits) : (0).toFixed(digits);
      }

      function formatUser(user) {
        if (!user) {
          return 'system';
        }
        return user.startsWith('U') ? `@${user}` : user;
      }

      function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char =>
          ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]
        );
      }

      function renderState(data) {
        const systemDiv = document.getElementById('state');
        const kpiDiv = document.getElementById('kpi');
        const lastDiv = document.getElementById('last-action');

        const connectivity = data.connectivity || {};
        const slackRaw = connectivity.slack;
        const slackStatus = slackRaw ? String(slackRaw).toUpperCase() : 'N/A';
        const statusItems = [
          ['Mode', data.mode ?? '-'],
          ['Paused', data.paused ? 'Ja' : 'Nein'],
          ['Heartbeat', data.heartbeat ?? '-'],
          ['Slack', slackStatus],
        ];
        systemDiv.innerHTML = statusItems
          .map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`)
          .join('');

        const risk = data.risk || {};
        const pnlDay = formatNumber(risk.pnl_day ?? 0);
        const pnlOpen = formatNumber(risk.pnl_open ?? 0);
        const marginUsed = formatPercent(risk.margin_used_pct ?? 0);
        kpiDiv.innerHTML = [
          ['PnL Day', pnlDay],
          ['PnL Open', pnlOpen],
          ['Margin Used', `${marginUsed}%`],
        ]
          .map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`)
          .join('');

        const last = data.last_action;
        if (last && last.action) {
          const actor = formatUser(last.user);
          const role = last.role ? ` (${last.role})` : '';
          lastDiv.textContent = `Letzte Aktion: ${last.action} durch ${actor}${role} um ${last.ts}`;
        } else {
          lastDiv.textContent = '';
        }
      }

      function renderClients(data) {
        const container = document.getElementById('clients');
        const connectivity = data.connectivity || {};
        const chips = Object.entries(connectivity)
          .map(([name, status]) => {
            const statusValue = String(status ?? 'unknown').toLowerCase();
            const safeStatus = ['up', 'down', 'unknown'].includes(statusValue) ? statusValue : 'unknown';
            return `<span class="chip ${safeStatus}">${escapeHtml(name)}: ${escapeHtml(safeStatus)}</span>`;
          })
          .join('');
        const clients = Array.isArray(data.clients) ? data.clients : [];
        const clientRows = clients
          .map(c => {
            const since = c.connected_at ? ` · seit ${escapeHtml(c.connected_at)}` : '';
            return `<div>${escapeHtml(c.id)} @ ${escapeHtml(c.remote || '?')}${since}</div>`;
          })
          .join('');
        const chipsHtml = chips || '<span class="chip unknown">Keine Daten</span>';
        const clientsHtml = clientRows || '<div>Keine aktiven Clients</div>';
        container.innerHTML = `
          <div class="connectivity-chips">${chipsHtml}</div>
          <div class="clients-list">${clientsHtml}</div>
        `;
      }

      function renderOrders(list) {
        const container = document.getElementById('orders');
        if (!Array.isArray(list) || list.length === 0) {
          container.textContent = 'Keine offenen Orders';
          return;
        }
        const rows = list
          .slice(0, 10)
          .map(o => {
            const ts = o.ts ? `${escapeHtml(o.ts)} ` : '';
            return `<div>${ts}${escapeHtml(o.symbol)} qty=${escapeHtml(o.qty)} px=${escapeHtml(o.px)} src=${escapeHtml(o.source)}</div>`;
          })
          .join('');
        container.innerHTML = rows;
      }

      let eventBuffer = [];
      function renderEvents(evts) {
        const container = document.getElementById('events');
        const newEvents = Array.isArray(evts) ? evts : [];
        eventBuffer = newEvents.concat(eventBuffer).slice(0, 25);
        container.innerHTML = eventBuffer
          .map(evt => {
            const payload = escapeHtml(JSON.stringify(evt.data ?? {}, null, 0));
            return `<div>${escapeHtml(evt.id)} ${escapeHtml(evt.topic)} [${escapeHtml(evt.level)}] ${payload}</div>`;
          })
          .join('');
      }

      function applyStatus(data) {
        renderState(data);
        renderClients(data);
        const orders = data.orders_open || data.orders || [];
        renderOrders(orders);
        if (data.events) {
          renderEvents(data.events);
        }
      }

      function fetchStatus() {
        fetch('/api/status', { headers: headers() })
          .then(r => {
            if (r.status === 401) {
              return r
                .json()
                .then(j => {
                  const reason = (j && j.reason) || 'unauthorized';
                  indicator.textContent = reason === 'role_denied' ? 'Role denied' : 'Unauthorized';
                  throw new Error(reason);
                })
                .catch(() => {
                  indicator.textContent = 'Unauthorized';
                  throw new Error('unauthorized');
                });
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
              return r
                .json()
                .then(j => {
                  const reason = (j && j.reason) || 'unauthorized';
                  indicator.textContent = reason === 'role_denied' ? 'Role denied' : 'Unauthorized';
                  throw new Error(reason);
                })
                .catch(() => {
                  indicator.textContent = 'Unauthorized';
                  throw new Error('unauthorized');
                });
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
        persistToken(currentToken);
        openSocket();
        fetchStatus();
      });

      tokenInput.addEventListener('input', () => {
        currentToken = tokenInput.value.trim();
        persistToken(currentToken);
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


@app.on_event("startup")
async def _on_startup() -> None:
    global _HEARTBEAT_TASK
    _record_dashboard_heartbeat()
    if _HEARTBEAT_TASK and not _HEARTBEAT_TASK.done():
        _HEARTBEAT_TASK.cancel()
        try:
            await _HEARTBEAT_TASK
        except asyncio.CancelledError:
            pass
    _HEARTBEAT_TASK = asyncio.create_task(_heartbeat_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    global _HEARTBEAT_TASK
    if _HEARTBEAT_TASK:
        _HEARTBEAT_TASK.cancel()
        try:
            await _HEARTBEAT_TASK
        except asyncio.CancelledError:
            pass
        _HEARTBEAT_TASK = None


def _parse_basic_auth(header: str) -> tuple[str, str]:
    if not header.lower().startswith("basic "):
        raise ValueError("unsupported auth scheme")
    encoded = header.split(" ", 1)[1].strip()
    try:
        decoded = b64decode(encoded).decode("utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("invalid basic auth header") from exc
    if ":" not in decoded:
        raise ValueError("invalid basic auth payload")
    user, password = decoded.split(":", 1)
    return user, password


def _require_token(request: Request) -> ControlIdentity:
    return _resolve_identity(
        header_token=request.headers.get("X-Dashboard-Token"),
        query_token=None,
        auth_header=request.headers.get("Authorization"),
        transport="http",
    )


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


def status_payload(last_action: dict[str, Any] | None = None) -> dict[str, Any]:
    global LAST_ACTION
    if last_action is not None:
        LAST_ACTION = last_action
    snapshot_last_action = LAST_ACTION

    state = read_state() or {}
    service_snapshot = get_services()
    services: dict[str, dict[str, Any]] = {}
    slack_detail = _load_slack_selftest_detail()
    now = time.time()
    connectivity: dict[str, str] = {}
    for name, info in service_snapshot.items():
        last_seen = float(info.get("last_seen", 0.0))
        state_flag = str(info.get("state") or "unknown")
        is_up = state_flag == "up" and now - last_seen <= HEALTH_WINDOW
        status = "up" if is_up else "down"
        entry: dict[str, Any] = {
            "state": state_flag,
            "last_seen": last_seen,
            "status": status,
        }
        details = info.get("details")
        if isinstance(details, dict):
            entry["details"] = details
        services[name] = entry
        connectivity[name] = status
    if slack_detail is not None and "slack" in services:
        services["slack"]["detail"] = slack_detail
    bus = Bus(settings.ipc_db)
    kpi = snapshot_kpis()
    orders = list_orders()
    events = bus.tail_events(limit=EVENT_LIMIT)
    clients = list(CLIENTS.values())
    heartbeat = datetime.now(UTC).isoformat(timespec="seconds") + "Z"

    risk_snapshot = kpi.get("risk") if isinstance(kpi, dict) else None
    risk_payload = {
        "pnl_day": float(risk_snapshot.get("pnl_day", 0.0)) if risk_snapshot else 0.0,
        "pnl_open": float(risk_snapshot.get("pnl_open", 0.0)) if risk_snapshot else 0.0,
        "margin_used_pct": float(risk_snapshot.get("margin_used_pct", 0.0))
        if risk_snapshot
        else 0.0,
    }

    return {
        "ok": True,
        "mode": state.get("mode") or ("mock" if state.get("mode_mock") else "real"),
        "paused": bool(state.get("paused", False)),
        "heartbeat": heartbeat,
        "connectivity": connectivity,
        "risk": risk_payload,
        "orders_open": orders,
        "orders": orders,
        "events": events,
        "clients": clients,
        "build": BUILD_INFO,
        "alerts": alert_counters(),
        "services": services,
        "kpi": kpi,
        "state": state,
        "last_action": snapshot_last_action,
    }


def _record_action(
    action: str, identity: ControlIdentity, status: str = "ok", **fields: Any
) -> None:
    METRICS.increment_counter("control.actions_total")
    log_event(
        "dashboard",
        f"control.{action}",
        "dashboard action executed",
        action=action,
        status=status,
        user=identity.user or identity.principal,
        role=identity.role,
        **fields,
    )
    Bus(settings.ipc_db).emit(
        "slack.notify",
        "INFO",
        {
            "type": "control-action",
            "action": action,
            "status": status,
            "ts": datetime.now(UTC).isoformat(timespec="seconds") + "Z",
            "user": identity.user or identity.principal,
            "role": identity.role,
            "fields": fields,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(content=INDEX_HTML)


@app.get("/healthz", response_class=JSONResponse)
async def healthz() -> dict[str, Any]:
    snapshot = get_services()
    now = time.time()
    statuses: dict[str, dict[str, Any]] = {}
    for name, info in snapshot.items():
        last_seen = float(info.get("last_seen", 0.0))
        state = str(info.get("state") or "unknown")
        status = "down"
        if state == "up" and now - last_seen <= HEALTH_WINDOW:
            status = "up"
        entry = {
            "state": state,
            "last_seen": last_seen,
            "status": status,
        }
        details = info.get("details")
        if isinstance(details, dict):
            entry["details"] = details
        statuses[name] = entry
    return {
        "ok": True,
        "services": statuses,
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
async def api_status(_identity: ControlIdentity = Depends(_require_token)) -> JSONResponse:
    try:
        payload = status_payload()
        return JSONResponse(payload)
    except Exception as exc:  # pragma: no cover - defensive
        log_event("dashboard", "api.status", "status error", level="ERROR", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)})


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import logging
    import sys
    import uvicorn

    logger = logging.getLogger("centrix.dashboard.runner")
    settings_override = AppSettings()

    config = uvicorn.Config(
        "centrix.dashboard.server:create_app",
        host=settings_override.dashboard_host,
        port=settings_override.dashboard_port,
        factory=True,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("Dashboard stopped cleanly")
        sys.exit(0)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Dashboard server crashed")
        sys.exit(1)
    logger.info("Dashboard stopped cleanly")


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
async def control_endpoint(
    request: Request, identity: ControlIdentity = Depends(_require_token)
) -> JSONResponse:
    try:
        payload = await request.json()
    except ClientDisconnect:
        log_event("dashboard", "api.control", "client disconnected", level="WARN")
        return JSONResponse({"ok": False, "error": "client_disconnected"}, status_code=499)
    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise HTTPException(status_code=400, detail="action required")
    snapshot = api_control(action, identity=identity, body=payload)
    return JSONResponse(snapshot)


def _authorised_name(action: str) -> str | None:
    mapping = {
        "pause": "pause",
        "resume": "resume",
        "mode": "mode",
        "restart": "restart",
        "test-order": "order",
        "order": "order",
    }
    return mapping.get(action)


def _apply_control_action(
    action: str, payload: dict[str, Any], identity: ControlIdentity
) -> dict[str, Any]:
    required = _authorised_name(action)
    if required and not allow(required, identity.role):
        raise HTTPException(status_code=403, detail="forbidden")

    state = read_state() or {}
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
        service_spec = payload.get("service")
        if not service_spec:
            raise HTTPException(status_code=400, detail="service required")
        targets: list[str]
        if isinstance(service_spec, list):
            targets = [str(item) for item in service_spec if item]
        else:
            targets = _parse_targets(str(service_spec))
        reports = [_restart_service(name) for name in targets]
        result["restart"] = reports if len(reports) > 1 else reports[0]
    elif action == "order":
        symbol = payload.get("symbol")
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol required")
        try:
            qty_val = int(payload.get("qty", 0))
            px_val = float(payload.get("px", 0.0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid order payload") from exc
        if qty_val <= 0:
            raise HTTPException(status_code=400, detail="qty must be positive")
        if px_val < 0:
            raise HTTPException(status_code=400, detail="px must be non-negative")
        bus = Bus(settings.ipc_db)
        order_message = {
            "symbol": symbol,
            "qty": qty_val,
            "px": px_val,
            "source": identity.principal,
            "user": identity.user,
        }
        order_id = bus.enqueue("order.submit", order_message)
        add_order(order_message)
        token = request_approval(
            order_id,
            initiator=identity.user or identity.principal,
            ttl_s=settings.order_approval_ttl_sec,
        )
        result["order"] = {
            "id": order_id,
            "symbol": symbol,
            "qty": qty_val,
            "px": px_val,
            "token": token,
        }
    elif action == "test-order":
        order_payload = {
            "source": identity.principal,
            "symbol": payload.get("symbol", "DEMO"),
            "qty": payload.get("qty", 1),
            "px": payload.get("px", 0),
            "user": identity.user,
        }
        result["order"] = add_order(order_payload)
    else:
        raise HTTPException(status_code=400, detail="unknown action")

    _record_action(action, identity, **{k: v for k, v in result.items() if k != "action"})
    return {"status": "ok", **result}


def api_control(
    action: str, *, identity: ControlIdentity, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = body or {}
    outcome = _apply_control_action(action, payload, identity)
    details = {k: v for k, v in outcome.items() if k != "status"}
    ts = datetime.now(UTC).isoformat(timespec="seconds") + "Z"
    last_action = {
        "action": action,
        "status": outcome.get("status", "ok"),
        "user": identity.user or identity.principal,
        "role": identity.role,
        "ts": ts,
        "details": details,
    }
    return status_payload(last_action=last_action)


def _ws_authorized(websocket: WebSocket) -> ControlIdentity:
    return _resolve_identity(
        header_token=websocket.headers.get("X-Dashboard-Token"),
        query_token=websocket.query_params.get("token"),
        auth_header=websocket.headers.get("Authorization"),
        transport="websocket",
    )


def _client_snapshot(
    websocket: WebSocket,
    client_id: str,
    identity: ControlIdentity | None,
) -> dict[str, Any]:
    return {
        "id": client_id,
        "remote": getattr(websocket.client, "host", "?"),
        "connected_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "principal": identity.principal if identity else None,
        "user": identity.user if identity else None,
        "role": identity.role if identity else None,
    }


def _events_since(last_id: int | None) -> list[dict[str, Any]]:
    bus = Bus(settings.ipc_db)
    events = bus.tail_events(limit=EVENT_LIMIT)
    filtered = [evt for evt in events if last_id is None or evt["id"] > last_id]
    return filtered


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    try:
        identity = _ws_authorized(websocket)
    except DashboardUnauthorized:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client_id = secrets.token_hex(4)
    CLIENTS[client_id] = _client_snapshot(websocket, client_id, identity)
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
def _is_auth_required() -> bool:
    override = os.environ.get("DASHBOARD_AUTH_REQUIRED")
    if override is not None:
        normalised = override.strip().lower()
        if normalised in {"1", "true", "yes", "on"}:
            return True
        if normalised in {"0", "false", "no", "off", ""}:
            return False
    return bool(settings.dashboard_auth_required)


def _configured_dashboard_token() -> str | None:
    token = os.environ.get("DASHBOARD_AUTH_TOKEN")
    if token:
        return token
    return settings.dashboard_auth_token


def _auth_failure(
    reason: str,
    *,
    user: str | None,
    has_token: bool,
    transport: str,
) -> None:
    log_event(
        "dashboard",
        "auth",
        "reject",
        reason=reason,
        user=user,
        has_token=has_token,
        transport=transport,
    )
    raise DashboardUnauthorized(reason=reason, user=user, has_token=has_token)


def _extract_basic_credentials(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    try:
        return _parse_basic_auth(header)
    except ValueError:
        return None


def _resolve_identity(
    *,
    header_token: str | None,
    query_token: str | None,
    auth_header: str | None,
    transport: str,
) -> ControlIdentity:
    required = _is_auth_required()
    configured_token = _configured_dashboard_token()
    provided_tokens = tuple(token for token in (header_token, query_token) if token)
    has_token = bool(provided_tokens)

    if configured_token and any(token == configured_token for token in provided_tokens):
        return ControlIdentity(principal="dashboard", user="dashboard", role="admin")

    credentials = _extract_basic_credentials(auth_header)
    if credentials:
        user, secret = credentials
        expected_role = settings.slack_role_map.get(user)
        if expected_role and expected_role.lower() == secret.lower():
            role = expected_role.lower()
            if role in AUTH_ALLOWED_ROLES:
                return ControlIdentity(principal="slack", user=user, role=role)
            if required:
                _auth_failure(
                    "role_denied",
                    user=user,
                    has_token=has_token,
                    transport=transport,
                )
            # Not required: fall back to default identity
        elif required:
            _auth_failure(
                "missing_or_bad_token",
                user=user,
                has_token=has_token,
                transport=transport,
            )

    if not required:
        return ControlIdentity(principal="dashboard", user="dashboard", role="admin")

    failing_user = credentials[0] if credentials else None
    _auth_failure(
        "missing_or_bad_token",
        user=failing_user,
        has_token=has_token,
        transport=transport,
    )


def _record_dashboard_heartbeat() -> None:
    touch_service("dashboard", "up", {"pid": os.getpid()})


async def _heartbeat_loop() -> None:
    try:
        while True:
            _record_dashboard_heartbeat()
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        touch_service("dashboard", "down", {"reason": "shutdown", "pid": os.getpid()})
