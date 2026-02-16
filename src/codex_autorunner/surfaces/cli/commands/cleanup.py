from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.managed_processes import reap_managed_processes
from ....core.report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
    prune_report_directory,
)
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
        force: bool = typer.Option(
            False,
            "--force",
            help="Terminate managed processes even when owner is still running",
        ),
    ) -> None:
        """Reap stale CAR-managed subprocesses and clean up registry records."""
        engine = require_repo_config(repo, hub)
        summary = reap_managed_processes(engine.repo_root, dry_run=dry_run, force=force)
        prefix = "Dry run: " if dry_run else ""
        typer.echo(
            f"{prefix}killed {summary.killed}, signaled {summary.signaled}, removed {summary.removed} records, skipped {summary.skipped}"
        )

    @cleanup_app.command("reports")
    def cleanup_reports(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        max_history_files: int = typer.Option(
            DEFAULT_REPORT_MAX_HISTORY_FILES,
            "--max-history-files",
            min=0,
            help="Max non-stable report files to retain.",
        ),
        max_total_bytes: int = typer.Option(
            DEFAULT_REPORT_MAX_TOTAL_BYTES,
            "--max-total-bytes",
            min=0,
            help="Max total bytes to retain under .codex-autorunner/reports.",
        ),
    ) -> None:
        """Prune report artifacts under .codex-autorunner/reports."""
        engine = require_repo_config(repo, hub)
        reports_dir = engine.repo_root / ".codex-autorunner" / "reports"
        summary = prune_report_directory(
            reports_dir,
            max_history_files=max_history_files,
            max_total_bytes=max_total_bytes,
        )
        typer.echo(
            "Reports cleanup: "
            f"kept={summary.kept} pruned={summary.pruned} "
            f"bytes_before={summary.bytes_before} bytes_after={summary.bytes_after}"
        )
