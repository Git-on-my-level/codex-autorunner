"""Backward-compatible CLI entrypoint.

Re-export the Typer app from the CLI surface.
"""

from .surfaces.cli.cli import _resolve_repo_api_path, app, main  # noqa: F401

__all__ = ["app", "main", "_resolve_repo_api_path"]


if __name__ == "__main__":  # pragma: no cover
    # The hub-local shim executes `python -m codex_autorunner.cli ...`.
    # This module is primarily a re-export layer, but it must be runnable so
    # basic commands like `--help`/`--version` produce output.
    main()
