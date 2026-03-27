from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.archive_retention import (
    prune_run_archive_root,
    prune_worktree_archive_root,
    resolve_run_archive_retention_policy,
    resolve_worktree_archive_retention_policy,
)
from ....core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.managed_processes import reap_managed_processes
from ....core.report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
    prune_report_directory,
)
from ....core.runtime import RuntimeContext
from ....core.state_retention import (
    CleanupResult,
    RetentionBucket,
    RetentionClass,
    RetentionScope,
    adapt_archive_prune_summary_to_result,
    adapt_filebox_prune_summary_to_result,
    adapt_report_prune_summary_to_result,
    aggregate_cleanup_results,
)
from ....integrations.app_server.retention import (
    adapt_workspace_summary_to_result,
    prune_workspace_root,
    resolve_global_workspace_root,
    resolve_repo_workspace_root,
    resolve_workspace_retention_policy,
)


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
        force_attestation_payload: Optional[dict[str, str]] = None
        if force:
            force_attestation_payload = _build_force_attestation(
                force_attestation,
                target_scope=f"cleanup.processes:{engine.repo_root}",
            )
        summary = reap_managed_processes(
            engine.repo_root,
            dry_run=dry_run,
            force=force,
            force_attestation=force_attestation_payload,
        )
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

        outputs: list[str] = []
        if scope_value in {"worktrees", "both"}:
            worktree_summary = prune_worktree_archive_root(
                engine.repo_root / ".codex-autorunner" / "archive" / "worktrees",
                policy=resolve_worktree_archive_retention_policy(engine.config.pma),
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
                policy=resolve_run_archive_retention_policy(engine.config.pma),
                dry_run=dry_run,
            )
            outputs.append(
                "runs: "
                f"kept={run_summary.kept} pruned={run_summary.pruned} "
                f"bytes_before={run_summary.bytes_before} bytes_after={run_summary.bytes_after}"
            )
        prefix = "Dry run: " if dry_run else ""
        typer.echo(prefix + " | ".join(outputs))

    @cleanup_app.command("filebox")
    def cleanup_filebox(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        scope: str = typer.Option(
            "both",
            "--scope",
            help="FileBox scope to prune: inbox, outbox, or both.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview FileBox pruning without deleting files."
        ),
    ) -> None:
        """Prune stale FileBox files under .codex-autorunner/filebox."""
        engine = require_repo_config(repo, hub)
        scope_value = scope.strip().lower()
        if scope_value not in {"inbox", "outbox", "both"}:
            raise typer.BadParameter("scope must be one of: inbox, outbox, both")
        summary = prune_filebox_root(
            engine.repo_root,
            policy=resolve_filebox_retention_policy(engine.config.pma),
            scope=scope_value,
            dry_run=dry_run,
        )
        prefix = "Dry run: " if dry_run else ""
        typer.echo(
            prefix
            + " | ".join(
                [
                    f"inbox: kept={summary.inbox_kept} pruned={summary.inbox_pruned}",
                    f"outbox: kept={summary.outbox_kept} pruned={summary.outbox_pruned}",
                    f"bytes_before={summary.bytes_before}",
                    f"bytes_after={summary.bytes_after}",
                ]
            )
        )

    @cleanup_app.command("state")
    def cleanup_state(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview cleanup without deleting files."
        ),
        scope: str = typer.Option(
            "repo",
            "--scope",
            help="Cleanup scope: repo, global, or all.",
        ),
    ) -> None:
        """Umbrella cleanup for CAR state retention across all families."""
        engine = require_repo_config(repo, hub)
        scope_value = scope.strip().lower()
        if scope_value not in {"repo", "global", "all"}:
            raise typer.BadParameter("scope must be one of: repo, global, all")

        results: list[CleanupResult] = []

        if scope_value in {"repo", "all"}:
            _run_repo_cleanup(engine, dry_run, results)

        if scope_value in {"global", "all"}:
            _run_global_cleanup(engine, dry_run, results)

        _print_cleanup_report(results, dry_run)

    def _run_repo_cleanup(engine: RuntimeContext, dry_run: bool, results: list) -> None:
        repo_root = engine.repo_root

        worktree_archive_bucket = RetentionBucket(
            family="worktree_archives",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        worktree_summary = prune_worktree_archive_root(
            repo_root / ".codex-autorunner" / "archive" / "worktrees",
            policy=resolve_worktree_archive_retention_policy(engine.config.pma),
            dry_run=dry_run,
        )
        results.append(
            adapt_archive_prune_summary_to_result(
                worktree_summary, worktree_archive_bucket, dry_run=dry_run
            )
        )

        run_archive_bucket = RetentionBucket(
            family="run_archives",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        run_summary = prune_run_archive_root(
            repo_root / ".codex-autorunner" / "archive" / "runs",
            policy=resolve_run_archive_retention_policy(engine.config.pma),
            dry_run=dry_run,
        )
        results.append(
            adapt_archive_prune_summary_to_result(
                run_summary, run_archive_bucket, dry_run=dry_run
            )
        )

        filebox_bucket = RetentionBucket(
            family="filebox",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        filebox_summary = prune_filebox_root(
            repo_root,
            policy=resolve_filebox_retention_policy(engine.config.pma),
            scope="both",
            dry_run=dry_run,
        )
        results.append(
            adapt_filebox_prune_summary_to_result(
                filebox_summary, filebox_bucket, dry_run=dry_run
            )
        )

        reports_bucket = RetentionBucket(
            family="reports",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.REVIEWABLE,
        )
        reports_dir = repo_root / ".codex-autorunner" / "reports"
        report_summary = prune_report_directory(
            reports_dir,
            max_history_files=DEFAULT_REPORT_MAX_HISTORY_FILES,
            max_total_bytes=DEFAULT_REPORT_MAX_TOTAL_BYTES,
        )
        results.append(
            adapt_report_prune_summary_to_result(report_summary, reports_bucket)
        )

        repo_workspace_bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        repo_workspace_root = resolve_repo_workspace_root(repo_root)
        repo_workspace_summary = prune_workspace_root(
            repo_workspace_root,
            policy=resolve_workspace_retention_policy(engine.config.pma),
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            dry_run=dry_run,
            scope=RetentionScope.REPO,
        )
        results.append(
            adapt_workspace_summary_to_result(
                repo_workspace_summary, repo_workspace_bucket, dry_run=dry_run
            )
        )

    def _run_global_cleanup(
        engine: RuntimeContext, dry_run: bool, results: list
    ) -> None:
        global_workspace_bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.GLOBAL,
            retention_class=RetentionClass.EPHEMERAL,
        )
        global_workspace_root = resolve_global_workspace_root()
        global_workspace_summary = prune_workspace_root(
            global_workspace_root,
            policy=resolve_workspace_retention_policy(engine.config.pma),
            active_workspace_ids=set(),
            locked_workspace_ids=set(),
            current_workspace_ids=set(),
            dry_run=dry_run,
            scope=RetentionScope.GLOBAL,
        )
        results.append(
            adapt_workspace_summary_to_result(
                global_workspace_summary, global_workspace_bucket, dry_run=dry_run
            )
        )

    def _print_cleanup_report(results: list, dry_run: bool) -> None:
        aggregated = aggregate_cleanup_results(results)
        prefix = "DRY RUN: " if dry_run else ""

        lines = [f"{prefix}CAR State Cleanup Report"]
        lines.append("=" * 50)

        by_family = {}
        for result in results:
            family = result.bucket.family
            if family not in by_family:
                by_family[family] = {
                    "deleted_count": 0,
                    "deleted_bytes": 0,
                    "kept_bytes": 0,
                    "blocked_count": len(result.plan.blocked_candidates),
                }
            by_family[family]["deleted_count"] += result.deleted_count
            by_family[family]["deleted_bytes"] += result.deleted_bytes
            by_family[family]["kept_bytes"] += result.kept_bytes

        for family, stats in sorted(by_family.items()):
            deleted_count = stats["deleted_count"]
            deleted_bytes = stats["deleted_bytes"]
            blocked_count = stats["blocked_count"]
            if deleted_count > 0 or blocked_count > 0:
                lines.append(f"{family}:")
                lines.append(f"  pruned={deleted_count} bytes={deleted_bytes}")
                if blocked_count > 0:
                    lines.append(f"  blocked={blocked_count}")

        lines.append("")
        lines.append(
            f"Total: deleted={aggregated.total_deleted_count} bytes={aggregated.total_deleted_bytes}"
        )
        if aggregated.has_errors:
            lines.append("")
            lines.append("Errors:")
            for error in aggregated.all_errors:
                lines.append(f"  - {error}")

        typer.echo("\n".join(lines))
