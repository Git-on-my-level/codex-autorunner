"""Typed plans and renderers for operational cleanup CLI commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from ...core.state_retention import (
    CleanupResult,
    aggregate_cleanup_results,
    summarize_cleanup_plan_lifecycle,
)


@dataclass(frozen=True)
class CleanupStatePlan:
    scope: Literal["repo", "global", "all"]
    dry_run: bool

    @classmethod
    def parse(cls, *, scope: str, dry_run: bool) -> "CleanupStatePlan":
        scope_value = scope.strip().lower()
        if scope_value not in {"repo", "global", "all"}:
            raise ValueError("scope must be one of: repo, global, all")
        return cls(scope=scope_value, dry_run=dry_run)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CleanupStateResult:
    plan: CleanupStatePlan
    results: tuple[CleanupResult, ...]

    def to_payload(self) -> dict[str, Any]:
        aggregated = aggregate_cleanup_results(self.results)
        return {
            "scope": self.plan.scope,
            "dry_run": self.plan.dry_run,
            "total_deleted_count": aggregated.total_deleted_count,
            "total_deleted_bytes": aggregated.total_deleted_bytes,
            "total_planned_count": sum(
                result.plan.prune_count for result in self.results
            ),
            "total_planned_bytes": sum(
                result.plan.reclaimable_bytes for result in self.results
            ),
            "errors": list(aggregated.all_errors),
            "buckets": [
                {
                    "scope": result.bucket.scope.value,
                    "family": result.bucket.family,
                    "planned_count": result.plan.prune_count,
                    "planned_bytes": result.plan.reclaimable_bytes,
                    "deleted_count": result.deleted_count,
                    "deleted_bytes": result.deleted_bytes,
                    "blocked_count": len(result.plan.blocked_candidates),
                }
                for result in self.results
            ],
        }


@dataclass(frozen=True)
class CleanupProcessesResult:
    summary: Any
    dry_run: bool


@dataclass(frozen=True)
class CleanupReportsResult:
    summary: Any


@dataclass(frozen=True)
class CleanupArchivesResult:
    outputs: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True)
class CleanupFileboxResult:
    summary: Any
    dry_run: bool


@dataclass(frozen=True)
class CleanupTempResult:
    label: Literal["pytest tmp cleanup", "temp root cleanup"]
    summary: Any
    dry_run: bool


@dataclass(frozen=True)
class HubCleanupPlan:
    dry_run: bool


@dataclass(frozen=True)
class HubCleanupResult:
    plan: HubCleanupPlan
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class HubRunsCleanupPlan:
    stale: bool
    older_than: str | None
    dry_run: bool
    delete_run: bool
    force: bool


@dataclass(frozen=True)
class HubRunsCleanupResult:
    plan: HubRunsCleanupPlan
    results: tuple[Mapping[str, Any], ...]
    errors: tuple[Mapping[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "dry_run": self.plan.dry_run,
            "stale": self.plan.stale,
            "older_than": self.plan.older_than,
            "delete_run": self.plan.delete_run,
            "force": self.plan.force,
            "results": list(self.results),
            "errors": list(self.errors),
        }


FlowHousekeepMode = Literal["stats", "dry_run", "execute"]


@dataclass(frozen=True)
class FlowHousekeepPlan:
    mode: FlowHousekeepMode
    retention_days: int
    run_id: str | None
    output_json: bool


@dataclass(frozen=True)
class FlowHousekeepResult:
    plan: FlowHousekeepPlan
    payload: Mapping[str, Any]
    errors: tuple[str, ...] = ()


def render_cleanup_state_human(result: CleanupStateResult) -> str:
    results = result.results
    dry_run = result.plan.dry_run
    aggregated = aggregate_cleanup_results(results)
    prefix = "DRY RUN: " if dry_run else ""

    lines: list[str] = []
    family_scope_counts: dict[str, set[str]] = {}
    for cleanup_result in results:
        family_scope_counts.setdefault(cleanup_result.bucket.family, set()).add(
            cleanup_result.bucket.scope.value
        )

    by_bucket: dict[tuple[str, str], dict[str, int]] = {}
    for cleanup_result in results:
        key = (cleanup_result.bucket.scope.value, cleanup_result.bucket.family)
        if key not in by_bucket:
            by_bucket[key] = {
                "action_count": 0,
                "action_bytes": 0,
                "blocked_count": 0,
            }
        action_count = (
            cleanup_result.plan.prune_count if dry_run else cleanup_result.deleted_count
        )
        action_bytes = (
            cleanup_result.plan.reclaimable_bytes
            if dry_run
            else cleanup_result.deleted_bytes
        )
        by_bucket[key]["action_count"] += action_count
        by_bucket[key]["action_bytes"] += action_bytes
        by_bucket[key]["blocked_count"] += len(cleanup_result.plan.blocked_candidates)

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
            action_label = "prune"
            matching_result = next(
                (
                    cleanup_result
                    for cleanup_result in results
                    if cleanup_result.bucket.family == family
                    and cleanup_result.bucket.scope.value == scope_name
                ),
                None,
            )
            if matching_result is not None:
                lifecycle = summarize_cleanup_plan_lifecycle(matching_result.plan)
                actions = lifecycle.get("actions", {})
                if actions:
                    action_label = ",".join(sorted(actions))
            lines.append(f"  pruned={action_count} bytes={action_bytes}")
            if action_label != "prune":
                lines.append(f"  lifecycle_actions={action_label}")
            if blocked_count > 0:
                lines.append(f"  blocked={blocked_count}")

    total_count = (
        sum(cleanup_result.plan.prune_count for cleanup_result in results)
        if dry_run
        else aggregated.total_deleted_count
    )
    total_bytes = (
        sum(cleanup_result.plan.reclaimable_bytes for cleanup_result in results)
        if dry_run
        else aggregated.total_deleted_bytes
    )
    lines.append(f"{prefix}total: pruned={total_count} bytes={total_bytes}")
    if aggregated.has_errors:
        lines.append("errors:")
        for error in aggregated.all_errors:
            lines.append(f"  {error}")
    return "\n".join(lines)


def render_cleanup_processes_human(result: CleanupProcessesResult) -> str:
    prefix = "dry-run: " if result.dry_run else ""
    summary = result.summary
    return (
        f"{prefix}killed={summary.killed} signaled={summary.signaled} "
        f"removed={summary.removed} skipped={summary.skipped}"
    )


def render_cleanup_reports_human(result: CleanupReportsResult) -> str:
    summary = result.summary
    return (
        f"reports: kept={summary.kept} pruned={summary.pruned} "
        f"bytes={summary.bytes_after}/{summary.bytes_before}"
    )


def render_cleanup_archives_human(result: CleanupArchivesResult) -> str:
    prefix = "Dry run: " if result.dry_run else ""
    return prefix + " | ".join(result.outputs)


def render_cleanup_filebox_human(result: CleanupFileboxResult) -> str:
    summary = result.summary
    prefix = "Dry run: " if result.dry_run else ""
    return prefix + " | ".join(
        [
            f"inbox: kept={summary.inbox_kept} pruned={summary.inbox_pruned}",
            f"outbox: kept={summary.outbox_kept} pruned={summary.outbox_pruned}",
            f"bytes_before={summary.bytes_before}",
            f"bytes_after={summary.bytes_after}",
        ]
    )


def render_cleanup_temp_human(result: CleanupTempResult) -> Sequence[str]:
    summary = result.summary
    prefix = "Dry run: " if result.dry_run else ""
    lines = [
        prefix
        + result.label
        + ": "
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
    ]
    lines.extend(f"ACTIVE {path}" for path in summary.active_paths)
    lines.extend(f"FAILED {detail}" for detail in summary.failed_paths)
    return lines


def render_hub_cleanup_json(result: HubCleanupResult, *, pretty: bool = False) -> str:
    return json.dumps(result.payload, indent=2 if pretty else None, default=str)


def render_hub_cleanup_human(result: HubCleanupResult) -> str:
    return str(result.payload.get("message", "Done"))


def render_hub_runs_cleanup_json(
    result: HubRunsCleanupResult, *, pretty: bool = False
) -> str:
    return json.dumps(result.to_payload(), indent=2 if pretty else None)


def render_hub_runs_cleanup_human(result: HubRunsCleanupResult) -> str:
    return (
        f"Hub runs cleanup candidates={len(result.results)} "
        f"errors={len(result.errors)} dry_run={result.plan.dry_run}"
    )


def render_flow_housekeep_json(result: FlowHousekeepResult) -> str:
    return json.dumps(dict(result.payload), indent=2)


def render_flow_housekeep_human(result: FlowHousekeepResult) -> Sequence[str]:
    payload = result.payload
    if result.plan.mode == "stats":
        lines = [
            f"db={payload['db_path']} size={payload['db_size_bytes']:,} "
            f"runs={payload['runs_total']}(active={payload['runs_active']},"
            f"terminal={payload['runs_terminal']},expired={payload['runs_expired']}) "
            f"events={payload['events_total']}(telemetry={payload['telemetry_total']},wire={payload['wire_events_total']}) "
            f"retention={payload['retention_days']}d"
        ]
        for run in payload["run_details"]:
            flags = []
            if run["is_active"]:
                flags.append("active")
            if run["is_terminal"]:
                flags.append("terminal")
            if run["is_expired"]:
                flags.append("expired")
            flag_str = ",".join(flags) if flags else "other"
            lines.append(
                f"  {run['run_id']}: {run['run_status']} [{flag_str}] "
                f"events={run['events_total']} telemetry={run['telemetry_total']} wire={run['wire_events']}"
            )
        return lines

    if result.plan.mode == "dry_run":
        lines = [
            f"housekeep(dry-run) retention={payload['retention_days']}d "
            f"process={payload['runs_to_process']} "
            f"skip_active={payload['runs_skipped_active']} "
            f"skip_not_expired={payload['runs_skipped_not_expired']} "
            f"export={payload['events_to_export']} prune={payload['events_to_prune']} "
            f"size={payload['estimated_export_bytes']:,} db={payload['db_size_bytes']:,}"
        ]
        for run in payload["run_details"]:
            lines.append(
                f"  {run['run_id']}: {run['run_status']} finished={run['finished_at']} "
                f"events={run['events_total']} wire={run['wire_events']}"
            )
        return lines

    lines = [
        f"housekeep: {payload['runs_processed']} runs "
        f"exported={payload['events_exported']}({payload['exported_bytes']:,} bytes) "
        f"pruned={payload['events_pruned']}"
    ]
    if payload.get("vacuum_performed"):
        lines.append("  vacuum: performed")
    lines.append(
        f"  db: {payload['db_size_before_bytes']:,} -> {payload['db_size_after_bytes']:,}"
    )
    return lines
