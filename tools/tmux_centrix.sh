#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="centrix"
VENV_DIR="${VENV:-.venv}"
PYTHON_BIN="${PYTHON:-$VENV_DIR/bin/python}"
LOG_FILE="runtime/logs/centrix.log"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required to launch the Centrix session" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Using existing tmux session '$SESSION_NAME'." >&2
  tmux attach -t "$SESSION_NAME"
  exit 0
fi

printf -v CONTROL_CMD 'PYTHONUNBUFFERED=1 %q -m centrix.tui.control' "$PYTHON_BIN"
printf -v DASHBOARD_CMD 'PYTHONUNBUFFERED=1 %q -m uvicorn centrix.dashboard.server:app --host 127.0.0.1 --port 8787' "$PYTHON_BIN"
printf -v WORKER_CMD 'PYTHONUNBUFFERED=1 %q -m centrix.services.confirm_worker' "$PYTHON_BIN"
printf -v LOGS_CMD 'tail -F %q' "$LOG_FILE"

tmux new-session -d -s "$SESSION_NAME" -n control "$CONTROL_CMD"

tmux new-window -t "$SESSION_NAME" -n dashboard "$DASHBOARD_CMD"

tmux new-window -t "$SESSION_NAME" -n worker "$WORKER_CMD"

tmux new-window -t "$SESSION_NAME" -n logs "$LOGS_CMD"

tmux select-window -t "$SESSION_NAME":control

tmux attach -t "$SESSION_NAME"
