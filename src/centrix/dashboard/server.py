"""FastAPI dashboard placeholder for Centrix."""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from .. import __version__
from ..settings import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_brand, version=__version__)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Health-check endpoint used by operators and systemd."""

    return {"ok": True}


def main() -> None:
    """Launch the dashboard service via uvicorn."""

    uvicorn.run(
        "centrix.dashboard.server:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
