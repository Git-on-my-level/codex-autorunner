from __future__ import annotations

from pathlib import Path

from codex_autorunner.core import ticket_flow_operator as operator_module
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.core.ticket_flow_operator import build_ticket_flow_run_state
from codex_autorunner.core.ticket_flow_recovery import (
    RecoveryIntentSeverity,
    RecoveryNotificationIntent,
)


def _create_running_ticket_flow(
    store: FlowStore, repo_root: Path, run_id: str, state: dict
) -> None:
    store.create_flow_run(
        run_id,
        "ticket_flow",
        input_data={"workspace_root": str(repo_root)},
        state=state,
        metadata={},
    )
    store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)


def test_recovery_projection_exposes_facets_from_flow_record(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        operator_module,
        "check_worker_health",
        lambda *_args, **_kwargs: FlowWorkerHealth(
            status="alive",
            pid=4242,
            cmdline=["car", "flow", "worker"],
            artifact_path=repo_root / ".codex-autorunner" / "flows" / run_id,
        ),
    )

    with FlowStore(db_path) as store:
        _create_running_ticket_flow(
            store,
            repo_root,
            run_id,
            {
                "recovery": {
                    "commit_barrier": {
                        "pending": True,
                        "barrier_epoch": "ticket-001-done",
                        "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
                    }
                }
            },
        )
        record = store.get_flow_run(run_id)
        assert record is not None

        run_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id="repo",
            record=record,
            store=store,
            has_pending_dispatch=False,
        )

    projection = run_state["recovery_projection"]
    assert projection["primary_state"] == "commit_barrier_pending"
    assert projection["attention_required"] is True
    assert projection["facets"]["commit_barrier"]["status"] == "active"
    assert projection["facets"]["restart"]["status"] == "clear"
    assert projection["facets"]["worker_health"]["status"] == "clear"
    assert run_state["notification_intents"][0]["event_type"] == (
        "ticket_flow.commit_barrier.active"
    )


def test_notification_intent_id_stable_when_restart_attempts_change(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(
        operator_module,
        "check_worker_health",
        lambda *_args, **_kwargs: FlowWorkerHealth(
            status="alive",
            pid=4242,
            cmdline=["car", "flow", "worker"],
            artifact_path=repo_root / ".codex-autorunner" / "flows" / run_id,
        ),
    )

    with FlowStore(db_path) as store:
        base_state = {
            "recovery": {
                "commit_barrier": {
                    "pending": True,
                    "barrier_epoch": "ticket-001-done",
                    "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
                },
                "restart": {"count": 1, "max_attempts": 3, "exhausted": False},
            }
        }
        _create_running_ticket_flow(store, repo_root, run_id, base_state)
        record = store.get_flow_run(run_id)
        assert record is not None
        first_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id="repo",
            record=record,
            store=store,
            has_pending_dispatch=False,
        )

        changed_state = {
            "recovery": {
                **base_state["recovery"],
                "restart": {"count": 2, "max_attempts": 3, "exhausted": False},
            }
        }
        record = store.update_flow_run_status(
            run_id,
            FlowRunStatus.RUNNING,
            state=changed_state,
        )
        assert record is not None
        second_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id="repo",
            record=record,
            store=store,
            has_pending_dispatch=False,
        )

    first_intent = first_state["notification_intents"][0]
    second_intent = second_state["notification_intents"][0]
    assert first_intent["event_type"] == "ticket_flow.commit_barrier.active"
    assert second_intent["event_type"] == "ticket_flow.commit_barrier.active"
    assert first_intent["intent_id"] == second_intent["intent_id"]


def test_notification_intent_ledger_upserts_observation_without_duplicate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "repo" / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = "33333333-3333-4333-8333-333333333333"
    intent = RecoveryNotificationIntent(
        intent_id="ticket_flow_recovery:test-intent",
        run_id=run_id,
        event_type="ticket_flow.commit_barrier.active",
        severity=RecoveryIntentSeverity.WARNING,
        reason="done-current-ticket-has-uncommitted-worktree-changes",
        recommended_actions=("car ticket-flow status --repo /tmp/repo",),
        cooldown_seconds=3600,
        payload={"facet": {"name": "commit_barrier"}},
    )

    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={},
            state={},
            metadata={},
        )
        first = store.upsert_notification_intent(
            intent, observed_at="2026-05-14T00:00:00+00:00"
        )
        second = store.upsert_notification_intent(
            intent, observed_at="2026-05-14T00:05:00+00:00"
        )
        rows = store.list_notification_intents(run_id=run_id)

    assert first.intent_id == second.intent_id
    assert second.first_seen_at == "2026-05-14T00:00:00+00:00"
    assert second.last_observed_at == "2026-05-14T00:05:00+00:00"
    assert second.observed_count == 2
    assert len(rows) == 1
