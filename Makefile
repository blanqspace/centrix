VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PIP ?= $(VENV)/bin/pip

.PHONY: init run-tui run-dashboard lint test fmt env-check

init:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .[dev]

run-tui:
	$(PYTHON) -m centrix.tui.control

run-dashboard:
	$(PYTHON) -m uvicorn centrix.dashboard.server:app --host 127.0.0.1 --port 8787

lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/mypy src

fmt:
	$(VENV)/bin/black src tests

test:
	$(VENV)/bin/pytest -q

env-check:
	$(PYTHON) tools/env_check.py
