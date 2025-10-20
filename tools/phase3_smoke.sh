#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv && . .venv/bin/activate
pip install -U pip >/dev/null
pip install -e .[dev] >/dev/null

mkdir -p runtime/logs runtime/pids runtime/locks runtime/reports
REPORT="runtime/reports/phase3.txt"
: > "$REPORT"

trap 'python -m centrix.cli svc stop all >/dev/null 2>&1 || true' EXIT

log_report() {
  echo "$1" | tee -a "$REPORT" >/dev/null
}

log_report "Phase 3 Smoke $(date -Iseconds)"

python -m centrix.cli svc start all | tee -a "$REPORT"
sleep 3

HEALTH_OUTPUT=$(curl -sS http://127.0.0.1:8787/healthz || echo "{}")
echo "$HEALTH_OUTPUT" | tee runtime/reports/healthz.json >/dev/null
log_report "healthz=$HEALTH_OUTPUT"

METRICS_OUTPUT=$(curl -sS http://127.0.0.1:8787/metrics || echo "{}")
echo "$METRICS_OUTPUT" | tee runtime/reports/metrics.json >/dev/null
log_report "metrics=$METRICS_OUTPUT"

log_report "Simulating alerts"
python - <<'PY'
from centrix.core.alerts import emit_alert
for _ in range(3):
    emit_alert("ERROR", "smoke.test", "simulated error", "phase3-demo")
PY

python - <<'PY' >> "$REPORT"
from pathlib import Path
import json
json_path = Path("runtime/logs/centrix.jsonl")
count = 0
if json_path.exists():
    for line in json_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if json.loads(line).get("topic") == "smoke.test":
            count += 1
print(f"alerts_emitted={count}")
PY

python - <<'PY' >> "$REPORT"
import json
from centrix.core.metrics import snapshot_kpis
print("metrics_snapshot=" + json.dumps(snapshot_kpis()))
PY

log_report "Lock lifecycle"
python - <<'PY'
import time
from centrix.shared.locks import acquire_lock
acquire_lock("phase3.lock", ttl=1)
time.sleep(2)
PY

python -m centrix.cli locks ls | tee -a "$REPORT"
python -m centrix.cli locks reap | tee -a "$REPORT"

log_report "Diagnostics snapshot"
SNAPSHOT_PATH=$(python -m centrix.cli diag snapshot | tee -a "$REPORT" | tail -n1)
if [[ -f "$SNAPSHOT_PATH" ]]; then
  log_report "diag_snapshot=$SNAPSHOT_PATH"
else
  log_report "diag_snapshot_missing"
fi

log_report "TUI smoke"
timeout 2s python -m centrix.tui.control >/dev/null 2>&1 || true

python -m centrix.cli svc stop all | tee -a "$REPORT"
log_report "Phase 3 smoke complete"
