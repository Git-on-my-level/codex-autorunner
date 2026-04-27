"""Backward-compatible CLI entrypoint.

Re-export the Typer app from the CLI surface.
"""

from .surfaces.cli.cli import app, main

__all__ = ["app", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
