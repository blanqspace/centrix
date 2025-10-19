"""Inter-process communication bus stubs."""
from __future__ import annotations

from typing import Any, Iterable


class Bus:
    """Placeholder IPC bus to be completed in later phases."""

    def connect(self) -> None:
        """Establish a connection to the bus."""
        # TODO: Implement persistent connection handling.

    def emit(self, topic: str, payload: Any) -> None:
        """Publish a payload on the given topic."""
        # TODO: Implement event fan-out and persistence.

    def tail(self, topic: str) -> Iterable[Any]:
        """Yield events from the given topic."""
        # TODO: Implement durable streaming consumption.
        return []
