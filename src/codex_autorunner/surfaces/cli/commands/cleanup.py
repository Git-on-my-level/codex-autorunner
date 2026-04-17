from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.archive_retention import (
    prune_run_archive_root,
    prune_worktree_archive_root,
    resolve_run_archive_retention_policy,
    resolve_worktree_archive_retention_policy,
)
from ....core.config import (
    ConfigError,
    default_housekeeping_rule_named,
    load_hub_config,
    resolve_housekeeping_rule,
)
from ....core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.locks import process_alive, process_command_matches
from ....core.managed_processes import reap_managed_processes
from ....core.managed_processes.registry import list_process_records
from ....core.pytest_temp_cleanup import (
    cleanup_repo_managed_temp_paths,
    cleanup_repo_pytest_temp_runs,
)
from ....core.report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
    prune_report_directory,
)
from ....core.runtime import RuntimeContext
from ....core.state_retention import (
    CleanupPlan,
    CleanupReason,
    CleanupResult,
    RetentionBucket,
    RetentionClass,
    RetentionScope,
    adapt_archive_prune_summary_to_result,
    adapt_filebox_prune_summary_to_result,
    adapt_housekeeping_rule_result_to_result,
    adapt_report_prune_summary_to_result,
    aggregate_cleanup_results,
)
from ....core.state_roots import resolve_global_state_root
from ....housekeeping import HousekeepingConfig, HousekeepingRule, run_housekeeping_once
from ....integrations.app_server.retention import (
    adapt_workspace_summary_to_result,
    prune_workspace_root,
    resolve_global_workspace_root,
    resolve_repo_workspace_root,
    resolve_workspace_retention_policy,
)
from ....manifest import load_manifest
from ....workspace import workspace_id_for_path
from ..hub_path_option import hub_root_path_option


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
        hub: Optional[Path] = hub_root_path_option(),
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
        prefix = "dry-run: " if dry_run else ""
        typer.echo(
            f"{prefix}killed={summary.killed} signaled={summary.signaled} removed={summary.removed} skipped={summary.skipped}"
        )

    @cleanup_app.command("reports")
    def cleanup_reports(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
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
            f"reports: kept={summary.kept} pruned={summary.pruned} bytes={summary.bytes_after}/{summary.bytes_before}"
        )

    @cleanup_app.command("archives")
    def cleanup_archives(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
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
        hub: Optional[Path] = hub_root_path_option(),
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

    @cleanup_app.command("pytest-tmp")
    def cleanup_pytest_tmp(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Preview pytest temp cleanup without deleting inactive roots.",
        ),
    ) -> None:
        """Prune inactive repo-specific pytest temp roots under the shared cp-* tree."""
        engine = require_repo_config(repo, hub)
        summary = cleanup_repo_pytest_temp_runs(engine.repo_root, dry_run=dry_run)
        prefix = "Dry run: " if dry_run else ""
        typer.echo(
            prefix
            + "pytest tmp cleanup: "
            + " ".join(
                [
                    f"scanned={summary.scanned}",
                    f"deleted={summary.deleted}",
                    f"active={summary.active}",
                    f"failed={summary.failed}",
                    f"bytes_before={summary.bytes_before}",
                    f"bytes_after={summary.bytes_after}",
                ]
            )
        )
        for path in summary.active_paths:
            typer.echo(f"ACTIVE {path}")
        for detail in summary.failed_paths:
            typer.echo(f"FAILED {detail}")

    @cleanup_app.command("temp-roots")
    def cleanup_temp_roots(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Preview CAR temp-root cleanup without deleting inactive roots.",
        ),
    ) -> None:
        """Prune stale CAR-managed temp roots outside repo-local state."""
        engine = require_repo_config(repo, hub)
        summary = cleanup_repo_managed_temp_paths(engine.repo_root, dry_run=dry_run)
        prefix = "Dry run: " if dry_run else ""
        typer.echo(
            prefix
            + "temp root cleanup: "
            + " ".join(
                [
                    f"scanned={summary.scanned}",
                    f"deleted={summary.deleted}",
                    f"active={summary.active}",
                    f"failed={summary.failed}",
                    f"bytes_before={summary.bytes_before}",
                    f"bytes_after={summary.bytes_after}",
                ]
            )
        )
        for path in summary.active_paths:
            typer.echo(f"ACTIVE {path}")
        for detail in summary.failed_paths:
            typer.echo(f"FAILED {detail}")

    @cleanup_app.command("state")
    def cleanup_state(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
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
        _run_repo_housekeeping_cleanup(engine, dry_run, results)

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
            max_history_files=_report_max_history_files(engine),
            max_total_bytes=_report_max_total_bytes(engine),
            dry_run=dry_run,
        )
        results.append(
            adapt_report_prune_summary_to_result(
                report_summary, reports_bucket, dry_run=dry_run
            )
        )

        repo_workspace_bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.REPO,
            retention_class=RetentionClass.EPHEMERAL,
        )
        repo_workspace_root = resolve_repo_workspace_root(repo_root)
        active_workspace_ids, locked_workspace_ids, current_workspace_ids = (
            _resolve_workspace_guards(
                engine, repo_workspace_root, scope=RetentionScope.REPO
            )
        )
        repo_workspace_summary = prune_workspace_root(
            repo_workspace_root,
            policy=resolve_workspace_retention_policy(engine.config.pma),
            active_workspace_ids=active_workspace_ids,
            locked_workspace_ids=locked_workspace_ids,
            current_workspace_ids=current_workspace_ids,
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
        _run_global_housekeeping_cleanup(engine, dry_run, results)

        global_workspace_bucket = RetentionBucket(
            family="workspaces",
            scope=RetentionScope.GLOBAL,
            retention_class=RetentionClass.EPHEMERAL,
        )
        global_workspace_root = _resolve_global_cleanup_workspace_root(engine)
        try:
            active_workspace_ids, locked_workspace_ids, current_workspace_ids = (
                _resolve_workspace_guards(
                    engine, global_workspace_root, scope=RetentionScope.GLOBAL
                )
            )
        except RuntimeError as exc:
            results.append(
                _make_skipped_cleanup_result(
                    global_workspace_bucket,
                    error=str(exc),
                )
            )
            return
        global_workspace_summary = prune_workspace_root(
            global_workspace_root,
            policy=resolve_workspace_retention_policy(engine.config.pma),
            active_workspace_ids=active_workspace_ids,
            locked_workspace_ids=locked_workspace_ids,
            current_workspace_ids=current_workspace_ids,
            dry_run=dry_run,
            scope=RetentionScope.GLOBAL,
        )
        results.append(
            adapt_workspace_summary_to_result(
                global_workspace_summary, global_workspace_bucket, dry_run=dry_run
            )
        )

    def _run_repo_housekeeping_cleanup(
        engine: RuntimeContext, dry_run: bool, results: list
    ) -> None:
        _run_housekeeping_cleanup(
            engine,
            dry_run,
            results,
            specs=(
                (
                    "run_logs",
                    RetentionBucket(
                        family="logs",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.EPHEMERAL,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
                (
                    "terminal_image_uploads",
                    RetentionBucket(
                        family="uploads",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.EPHEMERAL,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
                (
                    "telegram_images",
                    RetentionBucket(
                        family="uploads",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.EPHEMERAL,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
                (
                    "telegram_voice",
                    RetentionBucket(
                        family="uploads",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.EPHEMERAL,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
                (
                    "telegram_files",
                    RetentionBucket(
                        family="uploads",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.EPHEMERAL,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
                (
                    "github_context",
                    RetentionBucket(
                        family="github_context",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.REVIEWABLE,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
                (
                    "review_runs",
                    RetentionBucket(
                        family="review_runs",
                        scope=RetentionScope.REPO,
                        retention_class=RetentionClass.REVIEWABLE,
                    ),
                    CleanupReason.AGE_LIMIT,
                    True,
                    False,
                    None,
                ),
            ),
        )

    def _run_global_housekeeping_cleanup(
        engine: RuntimeContext, dry_run: bool, results: list
    ) -> None:
        _run_housekeeping_cleanup(
            engine,
            dry_run,
            results,
            specs=(
                (
                    "update_cache",
                    RetentionBucket(
                        family="update_cache",
                        scope=RetentionScope.GLOBAL,
                        retention_class=RetentionClass.CACHE_ONLY,
                    ),
                    CleanupReason.CACHE_REBUILDABLE,
                    False,
                    True,
                    lambda ctx: _resolve_global_cleanup_root(ctx) / "update_cache",
                ),
                (
                    "update_log",
                    RetentionBucket(
                        family="logs",
                        scope=RetentionScope.GLOBAL,
                        retention_class=RetentionClass.EPHEMERAL,
                    ),
                    CleanupReason.AGE_LIMIT,
                    False,
                    True,
                    lambda ctx: _resolve_global_cleanup_root(ctx)
                    / "update-standalone.log",
                ),
            ),
        )

    def _run_housekeeping_cleanup(
        engine: RuntimeContext,
        dry_run: bool,
        results: list,
        *,
        specs,
    ) -> None:
        base_config = getattr(engine.config, "housekeeping", None)
        resolved_specs = []
        resolved_rules: list[HousekeepingRule] = []

        for (
            rule_name,
            bucket,
            reason,
            include_repo_review_runs,
            include_hub_update_rules,
            path_resolver,
        ) in specs:
            rule = _resolve_cleanup_housekeeping_rule(
                engine,
                rule_name,
                include_repo_review_runs=include_repo_review_runs,
                include_hub_update_rules=include_hub_update_rules,
            )
            if rule is None:
                continue
            if path_resolver is not None:
                rule = dataclasses.replace(rule, path=str(path_resolver(engine)))
            resolved_specs.append((bucket, reason))
            resolved_rules.append(rule)

        if not resolved_rules:
            return

        summary = run_housekeeping_once(
            _cleanup_housekeeping_config(
                base_config,
                dry_run=dry_run,
                rules=resolved_rules,
            ),
            engine.repo_root,
        )
        for rule_result, (bucket, reason) in zip(summary.rules, resolved_specs):
            results.append(
                adapt_housekeeping_rule_result_to_result(
                    rule_result,
                    bucket,
                    dry_run=dry_run,
                    reason=reason,
                )
            )

    def _print_cleanup_report(results: list, dry_run: bool) -> None:
        aggregated = aggregate_cleanup_results(results)
        prefix = "DRY RUN: " if dry_run else ""

        lines = []

        family_scope_counts: dict[str, set[str]] = {}
        for result in results:
            family_scope_counts.setdefault(result.bucket.family, set()).add(
                result.bucket.scope.value
            )

        by_bucket = {}
        for result in results:
            key = (result.bucket.scope.value, result.bucket.family)
            if key not in by_bucket:
                by_bucket[key] = {
                    "action_count": 0,
                    "action_bytes": 0,
                    "blocked_count": 0,
                }
            action_count = result.plan.prune_count if dry_run else result.deleted_count
            action_bytes = (
                result.plan.reclaimable_bytes if dry_run else result.deleted_bytes
            )
            by_bucket[key]["action_count"] += action_count
            by_bucket[key]["action_bytes"] += action_bytes
            by_bucket[key]["blocked_count"] += len(result.plan.blocked_candidates)

        for (scope_name, family), stats in sorted(by_bucket.items()):
            action_count = stats["action_count"]
            action_bytes = stats["action_bytes"]
            blocked_count = stats["blocked_count"]
            if action_count > 0 or blocked_count > 0:
                label = (
                    f"{scope_name}/{family}"
                    if len(family_scope_counts.get(family, set())) > 1
                    else family
                )
                lines.append(f"{label}:")
                lines.append(f"  pruned={action_count} bytes={action_bytes}")
                if blocked_count > 0:
                    lines.append(f"  blocked={blocked_count}")

        total_count = (
            sum(result.plan.prune_count for result in results)
            if dry_run
            else aggregated.total_deleted_count
        )
        total_bytes = (
            sum(result.plan.reclaimable_bytes for result in results)
            if dry_run
            else aggregated.total_deleted_bytes
        )
        lines.append(f"{prefix}total: pruned={total_count} bytes={total_bytes}")
        if aggregated.has_errors:
            lines.append("errors:")
            for error in aggregated.all_errors:
                lines.append(f"  {error}")

        typer.echo("\n".join(lines))

    def _report_max_history_files(engine: RuntimeContext) -> int:
        return max(
            0,
            int(
                getattr(
                    engine.config.pma,
                    "report_max_history_files",
                    DEFAULT_REPORT_MAX_HISTORY_FILES,
                )
            ),
        )

    def _report_max_total_bytes(engine: RuntimeContext) -> int:
        return max(
            0,
            int(
                getattr(
                    engine.config.pma,
                    "report_max_total_bytes",
                    DEFAULT_REPORT_MAX_TOTAL_BYTES,
                )
            ),
        )

    def _resolve_workspace_guards(
        engine: RuntimeContext,
        workspace_root: Path,
        *,
        scope: RetentionScope,
    ) -> tuple[set[str], set[str], set[str]]:
        active_workspace_ids: set[str] = set()
        locked_workspace_ids = _workspace_ids_with_marker(
            workspace_root, marker_name="lock"
        )
        current_workspace_ids = _workspace_ids_with_marker(
            workspace_root, marker_name="run.json"
        )

        for managed_root in _managed_workspace_roots_for_guard_scan(
            engine, scope=scope
        ):
            active_workspace_ids.update(
                _live_workspace_ids_from_process_registry(managed_root)
            )
            thread_registry = (
                managed_root / ".codex-autorunner" / "app_server_threads.json"
            )
            if thread_registry.exists():
                current_workspace_ids.add(workspace_id_for_path(managed_root))

        return active_workspace_ids, locked_workspace_ids, current_workspace_ids

    def _managed_workspace_roots_for_guard_scan(
        engine: RuntimeContext, *, scope: RetentionScope
    ) -> tuple[Path, ...]:
        roots: set[Path] = {engine.repo_root.resolve()}
        if scope != RetentionScope.GLOBAL:
            return tuple(sorted(roots, key=lambda path: str(path)))

        try:
            hub_config = load_hub_config(engine.repo_root)
            manifest = load_manifest(hub_config.manifest_path, hub_config.root)
        except ConfigError as exc:
            raise RuntimeError(
                "Skipping global workspace cleanup: unable to load hub context for guard discovery; partial visibility could prune an active shared workspace."
            ) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                "Skipping global workspace cleanup: unable to inspect hub manifest for guard discovery; partial visibility could prune an active shared workspace."
            ) from exc

        for entry in manifest.repos:
            roots.add((hub_config.root / entry.path).resolve())
        for workspace in manifest.agent_workspaces:
            roots.add((hub_config.root / workspace.path).resolve())

        return tuple(sorted(roots, key=lambda path: str(path)))

    def _resolve_global_cleanup_workspace_root(engine: RuntimeContext) -> Path:
        app_server_cfg = getattr(engine.config, "app_server", None)
        state_root = getattr(app_server_cfg, "state_root", None)
        if isinstance(state_root, Path):
            return state_root
        return resolve_global_workspace_root(
            config=engine.config,
            repo_root=engine.repo_root,
        )

    def _live_workspace_ids_from_process_registry(repo_root: Path) -> set[str]:
        workspace_ids: set[str] = set()
        for record in list_process_records(repo_root, kind="codex_app_server"):
            workspace_id = str(record.workspace_id or "").strip()
            if not workspace_id or record.pid is None:
                continue
            if not process_alive(record.pid):
                continue
            if process_command_matches(record.pid, record.command) is False:
                continue
            workspace_ids.add(workspace_id)
        return workspace_ids

    def _workspace_ids_with_marker(root: Path, *, marker_name: str) -> set[str]:
        if not root.exists() or not root.is_dir():
            return set()
        protected_ids: set[str] = set()
        for path in root.iterdir():
            if not path.is_dir():
                continue
            if (path / marker_name).exists():
                protected_ids.add(path.name)
        return protected_ids

    def _resolve_global_cleanup_root(engine: RuntimeContext) -> Path:
        try:
            hub_config = load_hub_config(engine.repo_root)
        except ConfigError:
            hub_config = None
        if getattr(hub_config, "raw", None) is not None:
            return resolve_global_state_root(
                config=hub_config,
                repo_root=getattr(hub_config, "root", engine.repo_root),
            )
        return resolve_global_state_root(
            config=engine.config, repo_root=engine.repo_root
        )

    def _resolve_cleanup_housekeeping_rule(
        engine: RuntimeContext,
        rule_name: str,
        *,
        include_repo_review_runs: bool,
        include_hub_update_rules: bool,
    ):
        configured_housekeeping = getattr(engine.config, "housekeeping", None)
        configured_rule = resolve_housekeeping_rule(
            configured_housekeeping,
            rule_name,
        )
        if configured_rule is not None:
            return configured_rule
        if isinstance(configured_housekeeping, HousekeepingConfig):
            return None
        return default_housekeeping_rule_named(
            rule_name,
            include_repo_review_runs=include_repo_review_runs,
            include_hub_update_rules=include_hub_update_rules,
        )

    def _cleanup_housekeeping_config(
        base_config: object,
        *,
        dry_run: bool,
        rules: list[HousekeepingRule],
    ) -> HousekeepingConfig:
        if isinstance(base_config, HousekeepingConfig):
            return dataclasses.replace(
                base_config,
                enabled=True,
                dry_run=dry_run,
                rules=rules,
            )
        return HousekeepingConfig(
            enabled=True,
            interval_seconds=3600,
            min_file_age_seconds=600,
            dry_run=dry_run,
            rules=rules,
        )

    def _make_skipped_cleanup_result(
        bucket: RetentionBucket,
        *,
        error: str,
    ) -> CleanupResult:
        plan = CleanupPlan(
            bucket=bucket,
            candidates=(),
            total_bytes=0,
            reclaimable_bytes=0,
            kept_count=0,
            prune_count=0,
            blocked_count=0,
        )
        return CleanupResult(
            bucket=bucket,
            plan=plan,
            deleted_paths=(),
            deleted_count=0,
            deleted_bytes=0,
            kept_bytes=0,
            errors=(error,),
        )
