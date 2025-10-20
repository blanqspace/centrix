#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv && . .venv/bin/activate
pip install -U pip >/dev/null
pip install -e .[dev] >/dev/null

mkdir -p runtime/logs runtime/pids runtime/reports
REPORT="runtime/reports/phase4.txt"
: > "$REPORT"

log_report() {
  echo "$1" | tee -a "$REPORT" >/dev/null
}

trap 'python -m centrix.cli svc stop dashboard worker >/dev/null 2>&1 || true' EXIT

log_report "Phase 4 Smoke $(date -Iseconds)"
python -m centrix.cli svc start dashboard worker | tee -a "$REPORT"
sleep 2

curl -sS http://127.0.0.1:8787/healthz | tee runtime/reports/healthz.json >/dev/null
curl -sS http://127.0.0.1:8787/api/status | tee runtime/reports/status.json >/dev/null
curl -sS http://127.0.0.1:8787/metrics | tee runtime/reports/metrics.json >/dev/null

log_report "Control pause"
curl -sS -X POST http://127.0.0.1:8787/api/control \
  -H "Content-Type: application/json" \
  -d '{"action":"pause"}' | tee runtime/reports/control_pause.json >/dev/null
curl -sS http://127.0.0.1:8787/api/status | tee runtime/reports/status_after_pause.json >/dev/null

log_report "Control mode real"
curl -sS -X POST http://127.0.0.1:8787/api/control \
  -H "Content-Type: application/json" \
  -d '{"action":"mode","value":"real"}' | tee runtime/reports/control_mode.json >/dev/null

log_report "Control test-order"
curl -sS -X POST http://127.0.0.1:8787/api/control \
  -H "Content-Type: application/json" \
  -d '{"action":"test-order","symbol":"DEMO","qty":1,"px":0}' | tee runtime/reports/control_order.json >/dev/null

log_report "Control restart worker"
curl -sS -X POST http://127.0.0.1:8787/api/control \
  -H "Content-Type: application/json" \
  -d '{"action":"restart","service":"worker"}' | tee runtime/reports/control_restart.json >/dev/null

log_report "WebSocket sample"
python - <<'PY'
import asyncio
import json
from pathlib import Path

import websockets

async def main() -> None:
    uri = "ws://127.0.0.1:8787/ws"
    async with websockets.connect(uri) as ws:
        message = await ws.recv()
        Path("runtime/reports/ws_sample.json").write_text(message, encoding="utf-8")

asyncio.run(main())
PY

python -m centrix.cli svc stop dashboard worker | tee -a "$REPORT"
log_report "Phase 4 smoke complete"
