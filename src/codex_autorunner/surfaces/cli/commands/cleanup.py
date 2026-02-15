from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.managed_processes import reap_managed_processes
from ....core.runtime import RuntimeContext


def register_cleanup_commands(
    cleanup_app: typer.Typer,
    *,
    require_repo_config: Callable[[Optional[Path], Optional[Path]], RuntimeContext],
) -> None:
    @cleanup_app.command("processes")
    def cleanup_processes(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview without sending signals"
        ),
    ) -> None:
        """Reap stale CAR-managed subprocesses and clean up registry records."""
        engine = require_repo_config(repo, hub)
        summary = reap_managed_processes(engine.repo_root, dry_run=dry_run)
        prefix = "Dry run: " if dry_run else ""
        typer.echo(
            f"{prefix}killed {summary.killed}, removed {summary.removed} records, skipped {summary.skipped}"
        )
