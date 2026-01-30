"""Backward-compatible CLI entrypoint.

Re-export the Typer app from the CLI surface.
"""

from .surfaces.cli.cli import _resolve_repo_api_path, app, main  # noqa: F401

__all__ = ["app", "main", "_resolve_repo_api_path"]
