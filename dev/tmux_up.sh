#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${TMUX_SESSION_NAME:-centrix-dev}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ACTIVATE="$ROOT_DIR/.venv/bin/activate"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required to launch the Centrix development session" >&2
  exit 1
fi

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "Virtual environment not found at $VENV_ACTIVATE" >&2
  exit 1
fi

# Prepare Python environment for all panes.
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:${PYTHONPATH}}"

mkdir -p "$ROOT_DIR/runtime/logs"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  exec tmux attach -t "$SESSION_NAME"
fi

DASHBOARD_PORT="${DASHBOARD_PORT:-8787}"
printf -v CMD_SLACK 'cd %q && exec python %q' "$ROOT_DIR" "tools/run_slack.py"
printf -v CMD_WORKER 'cd %q && exec python %q' "$ROOT_DIR" "tools/run_worker.py"
printf -v CMD_DASHBOARD 'cd %q && exec uvicorn centrix.dashboard.server:app --host 0.0.0.0 --port %q' "$ROOT_DIR" "$DASHBOARD_PORT"
printf -v CMD_IBKR 'cd %q && exec PYTHONPATH=src python -m centrix.adapters.ibkr' "$ROOT_DIR"

tmux new-session -d -s "$SESSION_NAME" -n dev "$CMD_SLACK"
tmux split-window -v -t "${SESSION_NAME}:0" "$CMD_WORKER"
tmux select-pane -t "${SESSION_NAME}:0.0"
tmux split-window -h -t "${SESSION_NAME}:0" "$CMD_DASHBOARD"
tmux select-pane -t "${SESSION_NAME}:0.1"
tmux split-window -h -t "${SESSION_NAME}:0" "$CMD_IBKR"
tmux select-layout -t "${SESSION_NAME}:0" tiled
tmux select-pane -t "${SESSION_NAME}:0.0"

exec tmux attach -t "$SESSION_NAME"
