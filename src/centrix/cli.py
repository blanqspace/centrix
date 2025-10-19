"""Command-line interface entry point for Centrix."""
from __future__ import annotations

import typer

from . import __version__

app = typer.Typer(name="centrix", help="Centrix trading control platform CLI.")


@app.command()
def version() -> None:
    """Print the Centrix version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
