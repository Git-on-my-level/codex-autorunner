from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.archive_retention import (
    RunArchiveRetentionPolicy,
    WorktreeArchiveRetentionPolicy,
    prune_run_archive_root,
    prune_worktree_archive_root,
)
from ....core.config import DEFAULT_HUB_CONFIG
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.managed_processes import reap_managed_processes
from ....core.report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
    prune_report_directory,
)
from ....core.runtime import RuntimeContext


def _build_force_attestation(
    force_attestation: Optional[str], *, target_scope: str
) -> Optional[dict[str, str]]:
    if force_attestation is None:
        return None
    return {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": force_attestation,
        "target_scope": target_scope,
    }


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
        force_attestation: Optional[str] = typer.Option(
            None,
            "--force-attestation",
            help="Attestation text required with --force for dangerous actions.",
        ),
    ) -> None:
        """Reap stale CAR-managed subprocesses and clean up registry records."""
        engine = require_repo_config(repo, hub)
        reap_kwargs = {
            "dry_run": dry_run,
            "force": force,
        }
        force_attestation_payload: Optional[dict[str, str]] = None
        if force:
            force_attestation_payload = _build_force_attestation(
                force_attestation,
                target_scope=f"cleanup.processes:{engine.repo_root}",
            )
            reap_kwargs["force_attestation"] = force_attestation_payload
        summary = reap_managed_processes(engine.repo_root, **reap_kwargs)
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

    @cleanup_app.command("archives")
    def cleanup_archives(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        scope: str = typer.Option(
            "both",
            "--scope",
            help="Archive scope to prune: worktrees, runs, or both.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview archive pruning without deleting files."
        ),
    ) -> None:
        """Prune retained archive snapshots under .codex-autorunner/archive."""
        engine = require_repo_config(repo, hub)
        scope_value = scope.strip().lower()
        if scope_value not in {"worktrees", "runs", "both"}:
            raise typer.BadParameter("scope must be one of: worktrees, runs, both")

        defaults = DEFAULT_HUB_CONFIG.get("pma", {})
        raw = engine.config.raw if isinstance(engine.config.raw, dict) else {}
        pma = raw.get("pma") if isinstance(raw.get("pma"), dict) else {}

        outputs: list[str] = []
        if scope_value in {"worktrees", "both"}:
            worktree_summary = prune_worktree_archive_root(
                engine.repo_root / ".codex-autorunner" / "archive" / "worktrees",
                policy=WorktreeArchiveRetentionPolicy(
                    max_snapshots_per_repo=int(
                        pma.get(
                            "worktree_archive_max_snapshots_per_repo",
                            defaults.get("worktree_archive_max_snapshots_per_repo", 10),
                        )
                    ),
                    max_age_days=int(
                        pma.get(
                            "worktree_archive_max_age_days",
                            defaults.get("worktree_archive_max_age_days", 30),
                        )
                    ),
                    max_total_bytes=int(
                        pma.get(
                            "worktree_archive_max_total_bytes",
                            defaults.get(
                                "worktree_archive_max_total_bytes", 1_000_000_000
                            ),
                        )
                    ),
                ),
                dry_run=dry_run,
            )
            outputs.append(
                "worktrees: "
                f"kept={worktree_summary.kept} pruned={worktree_summary.pruned} "
                f"bytes_before={worktree_summary.bytes_before} bytes_after={worktree_summary.bytes_after}"
            )
        if scope_value in {"runs", "both"}:
            run_summary = prune_run_archive_root(
                engine.repo_root / ".codex-autorunner" / "archive" / "runs",
                policy=RunArchiveRetentionPolicy(
                    max_entries=int(
                        pma.get(
                            "run_archive_max_entries",
                            defaults.get("run_archive_max_entries", 200),
                        )
                    ),
                    max_age_days=int(
                        pma.get(
                            "run_archive_max_age_days",
                            defaults.get("run_archive_max_age_days", 30),
                        )
                    ),
                    max_total_bytes=int(
                        pma.get(
                            "run_archive_max_total_bytes",
                            defaults.get("run_archive_max_total_bytes", 1_000_000_000),
                        )
                    ),
                ),
                dry_run=dry_run,
            )
            outputs.append(
                "runs: "
                f"kept={run_summary.kept} pruned={run_summary.pruned} "
                f"bytes_before={run_summary.bytes_before} bytes_after={run_summary.bytes_after}"
            )
        prefix = "Dry run: " if dry_run else ""
        typer.echo(prefix + " | ".join(outputs))
