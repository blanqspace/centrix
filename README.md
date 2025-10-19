# Centrix

Centrix is a modular trading control platform. Phase 0 delivers a runnable foundation with a CLI,
placeholder dashboard, and TUI skeleton alongside tooling to support ongoing development.

## Getting Started

```bash
make init
cp .env.example .env
make run-tui
make run-dashboard
pytest -q
```

Refer to the `systemd/` and `tools/` directories for tmux and systemd helpers.
