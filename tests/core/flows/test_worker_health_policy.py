from __future__ import annotations

from codex_autorunner.core.flows.worker_health_policy import (
    WorkerHealthAction,
    WorkerHealthSnapshot,
    build_worker_health_snapshot,
    decide_worker_health,
)


def test_active_worker_over_wall_budget_requests_rotation_not_force_restart() -> None:
    decision = decide_worker_health(
        WorkerHealthSnapshot(
            process_status="alive",
            worker_age_seconds=7201,
            last_activity_at="2026-05-17T14:31:00+00:00",
            last_semantic_progress_at="2026-05-17T14:31:00+00:00",
            current_ticket=".codex-autorunner/tickets/TICKET-013.md",
            app_server_status="connected",
            idle_duration_seconds=5,
        ),
        max_wall_seconds=7200,
        idle_stale_seconds=1800,
    )

    assert decision.action == WorkerHealthAction.ROTATE_REQUESTED
    assert decision.exit_kind == "rotation_requested"
    assert decision.worker_health_severity == "warning"
    assert not decision.force_restart


def test_explicit_app_server_stall_forces_restart() -> None:
    decision = decide_worker_health(
        WorkerHealthSnapshot(
            process_status="alive",
            worker_age_seconds=30,
            app_server_status="stalled_timeout",
        ),
        max_wall_seconds=7200,
        idle_stale_seconds=1800,
    )

    assert decision.force_restart
    assert decision.reason == "app_server_stalled"
    assert decision.exit_kind == "app_server_stalled"


def test_idle_stale_alive_worker_forces_restart() -> None:
    snapshot = build_worker_health_snapshot(
        process_status="alive",
        last_activity_at="2026-05-17T14:00:00+00:00",
        now=None,
    )
    decision = decide_worker_health(
        WorkerHealthSnapshot(
            process_status=snapshot.process_status,
            last_activity_at=snapshot.last_activity_at,
            idle_duration_seconds=3600,
        ),
        max_wall_seconds=7200,
        idle_stale_seconds=1800,
    )

    assert decision.force_restart
    assert decision.reason == "idle_stale"
    assert decision.exit_kind == "idle_stale"


def test_missing_worker_forces_restart() -> None:
    decision = decide_worker_health(
        WorkerHealthSnapshot(process_status="absent"),
        max_wall_seconds=7200,
        idle_stale_seconds=1800,
    )

    assert decision.force_restart
    assert decision.reason == "missing_worker"
    assert decision.exit_kind == "missing_worker"
