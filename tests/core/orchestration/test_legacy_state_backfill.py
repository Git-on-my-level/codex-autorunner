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
from codex_autorunner.core.pma_thread_store import (
    _ensure_schema,
    default_pma_threads_db_path,
)
from codex_autorunner.core.sqlite_utils import open_sqlite


def _seed_legacy_thread_store(
    hub_root: Path, workspace_root: Path
) -> tuple[str, str, int]:
    thread_id = "thread-1"
    turn_id = "turn-1"
    action_id = 1
    legacy_path = default_pma_threads_db_path(hub_root)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    with open_sqlite(legacy_path) as conn:
        _ensure_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO pma_managed_threads (
                    managed_thread_id,
                    agent,
                    repo_id,
                    resource_kind,
                    resource_id,
                    workspace_root,
                    name,
                    backend_thread_id,
                    status,
                    normalized_status,
                    status_reason_code,
                    status_updated_at,
                    status_terminal,
                    status_turn_id,
                    last_turn_id,
                    last_message_preview,
                    compact_seed,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    "codex",
                    "repo-1",
                    "repo",
                    "repo-1",
                    str(workspace_root),
                    "Primary",
                    "backend-thread-1",
                    "completed",
                    "completed",
                    None,
                    "2026-03-13T00:00:02Z",
                    1,
                    turn_id,
                    turn_id,
                    "world",
                    None,
                    "{}",
                    "2026-03-13T00:00:00Z",
                    "2026-03-13T00:00:02Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO pma_managed_turns (
                    managed_turn_id,
                    managed_thread_id,
                    client_turn_id,
                    backend_turn_id,
                    prompt,
                    status,
                    assistant_text,
                    transcript_turn_id,
                    model,
                    reasoning,
                    error,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    thread_id,
                    "client-turn-1",
                    "backend-turn-1",
                    "hello",
                    "ok",
                    "world",
                    "transcript-turn-1",
                    "gpt-test",
                    "high",
                    None,
                    "2026-03-13T00:00:01Z",
                    "2026-03-13T00:00:02Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO pma_managed_actions (
                    action_id,
                    managed_thread_id,
                    action_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    thread_id,
                    "chat_completed",
                    '{"ok":true}',
                    "2026-03-13T00:00:03Z",
                ),
            )
    return thread_id, turn_id, action_id


def test_backfill_legacy_thread_state_imports_threads_turns_and_actions(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    thread_id, turn_id, action_id = _seed_legacy_thread_store(hub_root, workspace_root)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        counts = backfill_legacy_thread_state(hub_root, conn)
        thread_row = conn.execute(
            """
            SELECT *
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (thread_id,),
        ).fetchone()
        turn_row = conn.execute(
            """
            SELECT *
              FROM orch_thread_executions
             WHERE execution_id = ?
            """,
            (turn_id,),
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
    assert turn_row["thread_target_id"] == thread_id
    assert turn_row["request_kind"] == "managed_turn"
    assert turn_row["backend_turn_id"] == "backend-turn-1"
    assert turn_row["assistant_text"] == "world"
    assert action_row is not None
    assert action_row["thread_target_id"] == thread_id
    assert action_row["action_type"] == "chat_completed"


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
