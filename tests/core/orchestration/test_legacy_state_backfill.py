from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.orchestration.migrate_legacy_state import (
    backfill_legacy_automation_state,
    backfill_legacy_thread_state,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_automation_persistence import PmaAutomationPersistence
from codex_autorunner.core.pma_automation_types import default_pma_automation_state
from codex_autorunner.core.pma_thread_mirror import sync_legacy_mirror
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    _ensure_schema,
    _execution_row_to_record,
    _thread_row_to_record,
    default_pma_threads_db_path,
)


def test_backfill_legacy_thread_state_imports_threads_turns_and_actions(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)

    store = PmaThreadStore(hub_root)
    thread = store.create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        name="Primary",
        backend_thread_id="backend-thread-1",
    )
    turn = store.create_turn(
        thread["managed_thread_id"],
        prompt="hello",
        client_turn_id="client-turn-1",
        model="gpt-test",
        reasoning="high",
    )
    assert store.mark_turn_finished(
        turn["managed_turn_id"],
        status="ok",
        assistant_text="world",
        backend_turn_id="backend-turn-1",
        transcript_turn_id="transcript-turn-1",
    )
    action_id = store.append_action(
        "chat_completed",
        managed_thread_id=thread["managed_thread_id"],
        payload_json='{"ok":true}',
    )

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        sync_legacy_mirror(
            hub_root=hub_root,
            legacy_db_path=default_pma_threads_db_path(hub_root),
            durable=False,
            orchestration_conn=conn,
            thread_row_to_record=_thread_row_to_record,
            execution_row_to_record=_execution_row_to_record,
            ensure_legacy_schema=_ensure_schema,
        )
        with conn:
            conn.execute("DELETE FROM orch_thread_actions")
            conn.execute("DELETE FROM orch_thread_executions")
            conn.execute("DELETE FROM orch_thread_targets")
        counts = backfill_legacy_thread_state(hub_root, conn)
        thread_row = conn.execute(
            """
            SELECT *
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (thread["managed_thread_id"],),
        ).fetchone()
        turn_row = conn.execute(
            """
            SELECT *
              FROM orch_thread_executions
             WHERE execution_id = ?
            """,
            (turn["managed_turn_id"],),
        ).fetchone()
        action_row = conn.execute(
            """
            SELECT *
              FROM orch_thread_actions
             WHERE action_id = ?
            """,
            (str(action_id),),
        ).fetchone()

    assert counts == {"threads": 1, "turns": 1, "actions": 1}
    assert thread_row is not None
    assert thread_row["agent_id"] == "codex"
    assert thread_row["repo_id"] == "repo-1"
    assert thread_row["backend_thread_id"] == "backend-thread-1"
    assert thread_row["runtime_status"] == "completed"
    assert turn_row is not None
    assert turn_row["thread_target_id"] == thread["managed_thread_id"]
    assert turn_row["request_kind"] == "managed_turn"
    assert turn_row["backend_turn_id"] == "backend-turn-1"
    assert turn_row["assistant_text"] == "world"
    assert action_row is not None
    assert action_row["thread_target_id"] == thread["managed_thread_id"]
    assert action_row["action_type"] == "chat_completed"

    normalized_turn = PmaThreadStore(hub_root).get_turn(
        thread["managed_thread_id"], turn["managed_turn_id"]
    )
    assert normalized_turn is not None
    assert normalized_turn["request_kind"] == "message"


def test_backfill_legacy_automation_state_imports_json_store(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    persistence = PmaAutomationPersistence(hub_root)
    state = default_pma_automation_state()
    state["subscriptions"] = [
        {
            "subscription_id": "sub-1",
            "created_at": "2026-03-13T00:00:00Z",
            "updated_at": "2026-03-13T00:00:00Z",
            "state": "active",
            "event_types": ["flow_failed"],
            "repo_id": "repo-1",
            "run_id": "run-1",
            "thread_id": "thread-1",
            "lane_id": "pma:lane-1",
            "from_state": "running",
            "to_state": "failed",
            "reason": "manual_check",
            "idempotency_key": "sub-key-1",
            "max_matches": 1,
            "match_count": 0,
            "metadata": {"source": "legacy"},
        }
    ]
    state["timers"] = [
        {
            "timer_id": "timer-1",
            "due_at": "2026-03-13T01:00:00Z",
            "created_at": "2026-03-13T00:00:00Z",
            "updated_at": "2026-03-13T00:00:00Z",
            "state": "pending",
            "timer_type": "watchdog",
            "idle_seconds": 30,
            "subscription_id": "sub-1",
            "thread_id": "thread-1",
            "lane_id": "pma:lane-1",
            "reason": "watchdog_stalled",
            "idempotency_key": "timer-key-1",
            "metadata": {"mode": "legacy"},
        }
    ]
    state["wakeups"] = [
        {
            "wakeup_id": "wakeup-1",
            "created_at": "2026-03-13T00:10:00Z",
            "updated_at": "2026-03-13T00:10:00Z",
            "state": "pending",
            "source": "lifecycle_subscription",
            "repo_id": "repo-1",
            "run_id": "run-1",
            "thread_id": "thread-1",
            "lane_id": "pma:lane-1",
            "reason": "manual_check",
            "timestamp": "2026-03-13T00:10:00Z",
            "idempotency_key": "wake-key-1",
            "subscription_id": "sub-1",
            "timer_id": "timer-1",
            "event_id": "event-1",
            "event_type": "flow_failed",
            "event_data": {"severity": "high"},
            "metadata": {"origin": "legacy"},
        }
    ]
    persistence.save(state)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        counts = backfill_legacy_automation_state(hub_root, conn)
        sub_row = conn.execute(
            """
            SELECT *
              FROM orch_automation_subscriptions
             WHERE subscription_id = 'sub-1'
            """
        ).fetchone()
        timer_row = conn.execute(
            """
            SELECT *
              FROM orch_automation_timers
             WHERE timer_id = 'timer-1'
            """
        ).fetchone()
        wakeup_row = conn.execute(
            """
            SELECT *
              FROM orch_automation_wakeups
             WHERE wakeup_id = 'wakeup-1'
            """
        ).fetchone()

    assert counts == {"subscriptions": 1, "timers": 1, "wakeups": 1}
    assert sub_row is not None
    assert json.loads(str(sub_row["event_types_json"])) == ["flow_failed"]
    assert sub_row["thread_target_id"] == "thread-1"
    assert sub_row["idempotency_key"] == "sub-key-1"
    assert timer_row is not None
    assert timer_row["thread_target_id"] == "thread-1"
    assert timer_row["timer_kind"] == "watchdog"
    assert timer_row["idempotency_key"] == "timer-key-1"
    assert wakeup_row is not None
    assert wakeup_row["thread_target_id"] == "thread-1"
    assert wakeup_row["event_type"] == "flow_failed"
    assert wakeup_row["idempotency_key"] == "wake-key-1"
