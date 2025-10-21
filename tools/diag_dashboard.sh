#!/usr/bin/env bash
set -euo pipefail
. ./.venv/bin/activate || true
echo "Env:"
grep -E '^DASHBOARD_HOST=|^DASHBOARD_PORT=' .env || true
echo "Process:"
ps aux | grep -E 'uvicorn .*centrix.dashboard.server' | grep -v grep || true
echo "Health:"
curl -sv "http://${DASHBOARD_HOST:-127.0.0.1}:${DASHBOARD_PORT:-8787}/healthz" || true
