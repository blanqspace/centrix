#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv && . .venv/bin/activate
pip install -U pip >/dev/null
pip install -e .[dev] >/dev/null

mkdir -p runtime/logs runtime/pids runtime/locks runtime/reports
REPORT="runtime/reports/phase2.txt"
: > "$REPORT"
log_report() {
  echo "$1" | tee -a "$REPORT"
}

log_report "Phase 2 Smoke $(date -Iseconds)"

python -m centrix.cli svc stop all >/dev/null 2>&1 || true
python -m centrix.cli mode set mock | tee -a "$REPORT" >/dev/null
python -m centrix.cli state pause | tee -a "$REPORT" >/dev/null
python -m centrix.cli state resume | tee -a "$REPORT" >/dev/null

python -m centrix.cli svc start all | tee -a "$REPORT"
sleep 2
STATUS_ALL=$(python -m centrix.cli svc status)
echo "$STATUS_ALL" | tee -a "$REPORT"
if [[ "$STATUS_ALL" != *"running=3/3"* ]]; then
  log_report "Expected 3 services running"
  exit 1
fi

python -m centrix.cli svc stop dashboard | tee -a "$REPORT"
sleep 1
STATUS_MINUS=$(python -m centrix.cli svc status)
echo "$STATUS_MINUS" | tee -a "$REPORT"
if [[ "$STATUS_MINUS" != *"running=2/3"* ]]; then
  log_report "Expected 2 services running after dashboard stop"
  exit 1
fi

python -m centrix.cli svc start dashboard | tee -a "$REPORT"
sleep 1
STATUS_RESTORE=$(python -m centrix.cli svc status)
echo "$STATUS_RESTORE" | tee -a "$REPORT"
if [[ "$STATUS_RESTORE" != *"running=3/3"* ]]; then
  log_report "Expected 3 services running after dashboard restart"
  exit 1
fi

log_report "TUI smoke"
timeout 2s python -m centrix.tui.control >/dev/null 2>&1 || true

log_report "tmux detached"
TMUX= ./tools/tmux_centrix.sh --detached | tee -a "$REPORT"
TMUX= tmux kill-session -t centrix 2>/dev/null || true

log_report "Lock contention"
python - <<'PY' &
from centrix.shared.locks import acquire_lock, release_lock
import time
if acquire_lock("svc.control", ttl=30):
    time.sleep(3)
    release_lock("svc.control")
PY
LOCK_PID=$!
sleep 0.5
if python -m centrix.cli svc start all >/tmp/centrix_lock_test.txt 2>&1; then
  cat /tmp/centrix_lock_test.txt >> "$REPORT"
  log_report "Lock test failed"
  wait "$LOCK_PID"
  exit 1
fi
cat /tmp/centrix_lock_test.txt >> "$REPORT"
wait "$LOCK_PID"
rm -f /tmp/centrix_lock_test.txt

python -m centrix.cli svc stop all >/dev/null 2>&1 || true
log_report "Phase 2 smoke complete"
