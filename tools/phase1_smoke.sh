#!/usr/bin/env bash
set -euo pipefail

# venv
python -m venv .venv && . .venv/bin/activate
pip install -U pip
pip install -e .[dev]
cp -n .env.example .env || true
mkdir -p runtime/logs runtime/pids runtime/locks runtime/reports

python -V | tee runtime/reports/pyver.txt
pip list --format=freeze | tee runtime/reports/pip_freeze.txt

ruff check . | tee runtime/reports/ruff.txt
black --check . |& tee runtime/reports/black.txt
mypy src |& tee runtime/reports/mypy.txt
pytest -q |& tee runtime/reports/pytest.txt
python -m pip check |& tee runtime/reports/pip_check.txt
python -c "import centrix;print(\"centrix OK\")" |& tee -a runtime/reports/pkg_import.txt

echo "$(date -Iseconds) runtime ok" | tee -a runtime/logs/centrix.log

timeout 8s python -m centrix.dashboard.server |& tee runtime/reports/dashboard_run.txt & DASH_PID=$!
sleep 2
curl -sS http://127.0.0.1:8787/healthz | tee runtime/reports/healthz.json || true
kill ${DASH_PID} || true
wait ${DASH_PID} 2>/dev/null || true

timeout 5s python -m centrix.services.confirm_worker |& tee runtime/reports/worker_run.txt || true
timeout 3s python -m centrix.tui.control |& tee runtime/reports/tui_run.txt || true

systemd-analyze --user verify systemd/centrix-tui.service systemd/centrix-dashboard.service systemd/centrix-worker.service |& tee runtime/reports/systemd_verify.txt || true

chmod +x tools/tmux_centrix.sh
TMUX= tmux kill-session -t centrix 2>/dev/null || true
TMUX= timeout 8s ./tools/tmux_centrix.sh --detached |& tee runtime/reports/tmux_run.txt || true
TMUX= tmux kill-session -t centrix 2>/dev/null || true
