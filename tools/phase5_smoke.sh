#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv && . .venv/bin/activate
pip install -U pip >/dev/null
pip install -e .[dev] >/dev/null

mkdir -p runtime/logs runtime/pids runtime/reports
REPORT="runtime/reports/phase5.txt"
: > "$REPORT"

log_report() {
  echo "$1" | tee -a "$REPORT" >/dev/null
}

export SLACK_ENABLED=1
export SLACK_SIMULATION=1
export SLACK_BOT_TOKEN=""
export SLACK_APP_TOKEN=""

trap 'python -m centrix.cli svc stop slack,worker,dashboard >/dev/null 2>&1 || true' EXIT

log_report "Phase 5 Smoke $(date -Iseconds)"
python -m centrix.cli svc start slack,worker,dashboard | tee -a "$REPORT"
sleep 2

curl -sS http://127.0.0.1:8787/api/status | tee runtime/reports/phase5_status.json >/dev/null

python - <<'PY'
from centrix.services.slack import handle_slash_command, get_slack_out, SIM_LOG
from centrix.settings import get_settings

commands = [
    ("UADMIN", "status"),
    ("UADMIN", "pause"),
    ("UADMIN", "mode real"),
    ("UADMIN", "order DEMO 1 0"),
]
for user, cmd in commands:
    handle_slash_command(user, cmd)

get_slack_out()
print(SIM_LOG.exists())
PY

if [[ -f runtime/reports/slack_sim.jsonl ]]; then
  log_report "Slack simulation log present"
else
  log_report "Slack simulation log missing"
fi

python -m centrix.cli svc stop slack,worker,dashboard | tee -a "$REPORT"
log_report "Phase 5 smoke complete"
