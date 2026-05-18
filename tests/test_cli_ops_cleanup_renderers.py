from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.state_retention import (
    REPO_REPORTS_BUCKET,
    CleanupAction,
    CleanupCandidate,
    CleanupPlan,
    CleanupReason,
    CleanupResult,
)
from codex_autorunner.surfaces.cli.ops_cleanup import (
    CleanupStatePlan,
    CleanupStateResult,
    FlowHousekeepPlan,
    FlowHousekeepResult,
    HubCleanupPlan,
    HubCleanupResult,
    HubRunsCleanupPlan,
    HubRunsCleanupResult,
    render_cleanup_state_human,
    render_flow_housekeep_human,
    render_flow_housekeep_json,
    render_hub_cleanup_human,
    render_hub_cleanup_json,
    render_hub_runs_cleanup_human,
    render_hub_runs_cleanup_json,
)


def test_cleanup_state_renderer_reports_human_and_payload_totals() -> None:
    candidate = CleanupCandidate(
        path=Path("/tmp/report.json"),
        size_bytes=25,
        bucket=REPO_REPORTS_BUCKET,
        action=CleanupAction.PRUNE,
        reason=CleanupReason.AGE_LIMIT,
    )
    cleanup_plan = CleanupPlan(
        bucket=REPO_REPORTS_BUCKET,
        candidates=(candidate,),
        total_bytes=50,
        reclaimable_bytes=25,
        kept_count=1,
        prune_count=1,
        blocked_count=0,
    )
    result = CleanupStateResult(
        plan=CleanupStatePlan(scope="repo", dry_run=True),
        results=(
            CleanupResult(
                bucket=REPO_REPORTS_BUCKET,
                plan=cleanup_plan,
                deleted_paths=(),
                deleted_count=0,
                deleted_bytes=0,
                kept_bytes=25,
            ),
        ),
    )

    assert "DRY RUN: total: pruned=1 bytes=25" in render_cleanup_state_human(result)
    assert result.to_payload()["total_planned_count"] == 1
    assert result.to_payload()["buckets"][0]["family"] == "reports"


def test_hub_runs_cleanup_renderers_share_result_contract() -> None:
    result = HubRunsCleanupResult(
        plan=HubRunsCleanupPlan(
            stale=True,
            older_than="7d",
            dry_run=True,
            delete_run=False,
            force=False,
        ),
        results=({"repo_id": "repo-a", "run_id": "run-1"},),
        errors=(),
    )

    payload = json.loads(render_hub_runs_cleanup_json(result, pretty=True))

    assert payload["dry_run"] is True
    assert payload["delete_run"] is False
    assert payload["results"][0]["run_id"] == "run-1"
    assert render_hub_runs_cleanup_human(result) == (
        "Hub runs cleanup candidates=1 errors=0 dry_run=True"
    )


def test_hub_cleanup_renderers_share_result_contract() -> None:
    result = HubCleanupResult(
        plan=HubCleanupPlan(dry_run=True),
        payload={"message": "dry run complete", "dry_run": True},
    )

    assert json.loads(render_hub_cleanup_json(result))["dry_run"] is True
    assert render_hub_cleanup_human(result) == "dry run complete"


def test_flow_housekeep_renderers_share_result_contract() -> None:
    result = FlowHousekeepResult(
        plan=FlowHousekeepPlan(
            mode="dry_run",
            retention_days=14,
            run_id=None,
            output_json=True,
        ),
        payload={
            "dry_run": True,
            "retention_days": 14,
            "runs_to_process": 1,
            "runs_skipped_active": 2,
            "runs_skipped_not_expired": 3,
            "events_to_export": 4,
            "events_to_prune": 5,
            "estimated_export_bytes": 6000,
            "db_size_bytes": 7000,
            "run_details": [
                {
                    "run_id": "run-1",
                    "run_status": "completed",
                    "finished_at": "2026-01-01T00:00:00Z",
                    "events_total": 8,
                    "wire_events": 9,
                }
            ],
        },
    )

    payload = json.loads(render_flow_housekeep_json(result))
    human_lines = list(render_flow_housekeep_human(result))

    assert payload["retention_days"] == 14
    assert human_lines[0] == (
        "housekeep(dry-run) retention=14d process=1 skip_active=2 "
        "skip_not_expired=3 export=4 prune=5 size=6,000 db=7,000"
    )
    assert "run-1" in human_lines[1]
