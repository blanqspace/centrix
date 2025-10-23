"""Central logging configuration for Centrix."""

from __future__ import annotations

import logging
from typing import Any

DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO, *, fmt: str = DEFAULT_FORMAT, **kwargs: Any) -> None:
    """Ensure the root logger has at least one handler configured."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(level=level, format=fmt, **kwargs)
