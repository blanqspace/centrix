#!/usr/bin/env bash
set -euo pipefail
. .venv/bin/activate || true
[ -f .env ] || { echo ".env fehlt"; exit 2; }
# Harte Vorbedingung: Real-Mode
grep -q '^SLACK_ENABLED=1' .env || { echo "SLACK_ENABLED=1 nötig"; exit 2; }
grep -q '^SLACK_SIMULATION=0' .env || { echo "SLACK_SIMULATION=0 nötig"; exit 2; }

# Optional Dashboard für Statussicht
centrix svc start dashboard || true
sleep 2 || true

# Selftest
centrix slack:selftest
echo "Selftest abgeschlossen"
curl -sS http://127.0.0.1:8787/api/status | tee runtime/reports/status_after_slack_selftest.json || true
