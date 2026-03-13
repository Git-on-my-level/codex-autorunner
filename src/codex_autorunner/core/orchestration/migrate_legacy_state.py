from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..sqlite_utils import open_sqlite
from ..time_utils import now_iso

LEGACY_PMA_THREADS_DB_PATH = Path(".codex-autorunner/pma/threads.sqlite3")
LEGACY_PMA_AUTOMATION_PATH = Path(".codex-autorunner/pma/automation_store.json")
LEGACY_PMA_QUEUE_DIR = Path(".codex-autorunner/pma/queue")
LEGACY_PMA_REACTIVE_PATH = Path(".codex-autorunner/pma/reactive_state.json")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type = 'table'
           AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def backfill_legacy_thread_state(hub_root: Path, conn: Any) -> dict[str, int]:
    legacy_path = hub_root / LEGACY_PMA_THREADS_DB_PATH
    if not legacy_path.exists():
        return {"threads": 0, "turns": 0, "actions": 0}

    counts = {"threads": 0, "turns": 0, "actions": 0}
    with open_sqlite(legacy_path) as legacy_conn:
        if _table_exists(legacy_conn, "pma_managed_threads"):
            rows = legacy_conn.execute("SELECT * FROM pma_managed_threads").fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO orch_thread_targets (
                        thread_target_id,
                        agent_id,
                        backend_thread_id,
                        repo_id,
                        workspace_root,
                        display_name,
                        lifecycle_status,
                        runtime_status,
                        status_reason,
                        status_turn_id,
                        last_execution_id,
                        last_message_preview,
                        compact_seed,
                        created_at,
                        updated_at,
                        status_updated_at,
                        status_terminal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_target_id) DO UPDATE SET
                        agent_id = excluded.agent_id,
                        backend_thread_id = excluded.backend_thread_id,
                        repo_id = excluded.repo_id,
                        workspace_root = excluded.workspace_root,
                        display_name = excluded.display_name,
                        lifecycle_status = excluded.lifecycle_status,
                        runtime_status = excluded.runtime_status,
                        status_reason = excluded.status_reason,
                        status_turn_id = excluded.status_turn_id,
                        last_execution_id = excluded.last_execution_id,
                        last_message_preview = excluded.last_message_preview,
                        compact_seed = excluded.compact_seed,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        status_updated_at = excluded.status_updated_at,
                        status_terminal = excluded.status_terminal
                    """,
                    (
                        row["managed_thread_id"],
                        row["agent"],
                        row["backend_thread_id"],
                        row["repo_id"],
                        row["workspace_root"],
                        row["name"],
                        row["status"],
                        row["normalized_status"],
                        row["status_reason_code"],
                        row["status_turn_id"],
                        row["last_turn_id"],
                        row["last_message_preview"],
                        row["compact_seed"],
                        row["created_at"],
                        row["updated_at"],
                        row["status_updated_at"] or row["updated_at"],
                        int(row["status_terminal"] or 0),
                    ),
                )
            counts["threads"] = len(rows)

        if _table_exists(legacy_conn, "pma_managed_turns"):
            rows = legacy_conn.execute("SELECT * FROM pma_managed_turns").fetchall()
            for row in rows:
                created_at = row["started_at"] or row["finished_at"] or now_iso()
                conn.execute(
                    """
                    INSERT INTO orch_thread_executions (
                        execution_id,
                        thread_target_id,
                        client_request_id,
                        request_kind,
                        prompt_text,
                        status,
                        backend_turn_id,
                        assistant_text,
                        error_text,
                        model_id,
                        reasoning_level,
                        transcript_mirror_id,
                        started_at,
                        finished_at,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(execution_id) DO UPDATE SET
                        thread_target_id = excluded.thread_target_id,
                        client_request_id = excluded.client_request_id,
                        request_kind = excluded.request_kind,
                        prompt_text = excluded.prompt_text,
                        status = excluded.status,
                        backend_turn_id = excluded.backend_turn_id,
                        assistant_text = excluded.assistant_text,
                        error_text = excluded.error_text,
                        model_id = excluded.model_id,
                        reasoning_level = excluded.reasoning_level,
                        transcript_mirror_id = excluded.transcript_mirror_id,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        created_at = excluded.created_at
                    """,
                    (
                        row["managed_turn_id"],
                        row["managed_thread_id"],
                        row["client_turn_id"],
                        "managed_turn",
                        row["prompt"],
                        row["status"],
                        row["backend_turn_id"],
                        row["assistant_text"],
                        row["error"],
                        row["model"],
                        row["reasoning"],
                        row["transcript_turn_id"],
                        row["started_at"],
                        row["finished_at"],
                        created_at,
                    ),
                )
            counts["turns"] = len(rows)

        if _table_exists(legacy_conn, "pma_managed_actions"):
            rows = legacy_conn.execute("SELECT * FROM pma_managed_actions").fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO orch_thread_actions (
                        action_id,
                        thread_target_id,
                        execution_id,
                        action_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(action_id) DO UPDATE SET
                        thread_target_id = excluded.thread_target_id,
                        execution_id = excluded.execution_id,
                        action_type = excluded.action_type,
                        payload_json = excluded.payload_json,
                        created_at = excluded.created_at
                    """,
                    (
                        str(row["action_id"]),
                        row["managed_thread_id"],
                        None,
                        row["action_type"],
                        row["payload_json"] or "{}",
                        row["created_at"],
                    ),
                )
            counts["actions"] = len(rows)

    return counts


def backfill_legacy_automation_state(hub_root: Path, conn: Any) -> dict[str, int]:
    legacy_state = _load_json_file(hub_root / LEGACY_PMA_AUTOMATION_PATH)
    if legacy_state is None:
        return {"subscriptions": 0, "timers": 0, "wakeups": 0}

    subscriptions = legacy_state.get("subscriptions")
    timers = legacy_state.get("timers")
    wakeups = legacy_state.get("wakeups")
    sub_rows = subscriptions if isinstance(subscriptions, list) else []
    timer_rows = timers if isinstance(timers, list) else []
    wakeup_rows = wakeups if isinstance(wakeups, list) else []

    for entry in sub_rows:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata")
        conn.execute(
            """
            INSERT INTO orch_automation_subscriptions (
                subscription_id,
                event_types_json,
                repo_id,
                run_id,
                thread_target_id,
                binding_id,
                lane_id,
                from_state,
                to_state,
                notify_once,
                state,
                match_count,
                metadata_json,
                created_at,
                updated_at,
                disabled_at,
                reason_text,
                idempotency_key,
                max_matches
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                event_types_json = excluded.event_types_json,
                repo_id = excluded.repo_id,
                run_id = excluded.run_id,
                thread_target_id = excluded.thread_target_id,
                binding_id = excluded.binding_id,
                lane_id = excluded.lane_id,
                from_state = excluded.from_state,
                to_state = excluded.to_state,
                notify_once = excluded.notify_once,
                state = excluded.state,
                match_count = excluded.match_count,
                metadata_json = excluded.metadata_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                disabled_at = excluded.disabled_at,
                reason_text = excluded.reason_text,
                idempotency_key = excluded.idempotency_key,
                max_matches = excluded.max_matches
            """,
            (
                entry.get("subscription_id"),
                _json_dumps(entry.get("event_types") or []),
                entry.get("repo_id"),
                entry.get("run_id"),
                entry.get("thread_id"),
                None,
                entry.get("lane_id"),
                entry.get("from_state"),
                entry.get("to_state"),
                1 if entry.get("max_matches") == 1 else 0,
                entry.get("state") or "active",
                int(entry.get("match_count") or 0),
                _json_dumps(metadata if isinstance(metadata, dict) else {}),
                entry.get("created_at") or now_iso(),
                entry.get("updated_at") or entry.get("created_at") or now_iso(),
                (
                    entry.get("updated_at") or entry.get("created_at") or now_iso()
                    if entry.get("state") == "cancelled"
                    else None
                ),
                entry.get("reason"),
                entry.get("idempotency_key"),
                entry.get("max_matches"),
            ),
        )

    for entry in timer_rows:
        if not isinstance(entry, dict):
            continue
        payload = {
            "metadata": (
                entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            ),
            "from_state": entry.get("from_state"),
            "to_state": entry.get("to_state"),
        }
        conn.execute(
            """
            INSERT INTO orch_automation_timers (
                timer_id,
                subscription_id,
                repo_id,
                run_id,
                thread_target_id,
                timer_kind,
                schedule_key,
                available_at,
                payload_json,
                state,
                created_at,
                updated_at,
                fired_at,
                reason_text,
                idempotency_key,
                idle_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(timer_id) DO UPDATE SET
                subscription_id = excluded.subscription_id,
                repo_id = excluded.repo_id,
                run_id = excluded.run_id,
                thread_target_id = excluded.thread_target_id,
                timer_kind = excluded.timer_kind,
                schedule_key = excluded.schedule_key,
                available_at = excluded.available_at,
                payload_json = excluded.payload_json,
                state = excluded.state,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                fired_at = excluded.fired_at,
                reason_text = excluded.reason_text,
                idempotency_key = excluded.idempotency_key,
                idle_seconds = excluded.idle_seconds
            """,
            (
                entry.get("timer_id"),
                entry.get("subscription_id"),
                entry.get("repo_id"),
                entry.get("run_id"),
                entry.get("thread_id"),
                entry.get("timer_type") or "one_shot",
                entry.get("subscription_id") or entry.get("idempotency_key"),
                entry.get("due_at") or now_iso(),
                _json_dumps(payload),
                entry.get("state") or "pending",
                entry.get("created_at") or now_iso(),
                entry.get("updated_at") or entry.get("created_at") or now_iso(),
                entry.get("fired_at"),
                entry.get("reason"),
                entry.get("idempotency_key"),
                entry.get("idle_seconds"),
            ),
        )

    for entry in wakeup_rows:
        if not isinstance(entry, dict):
            continue
        payload = {
            "metadata": (
                entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            ),
            "event_data": (
                entry.get("event_data")
                if isinstance(entry.get("event_data"), dict)
                else {}
            ),
            "source": entry.get("source"),
            "from_state": entry.get("from_state"),
            "to_state": entry.get("to_state"),
        }
        conn.execute(
            """
            INSERT INTO orch_automation_wakeups (
                wakeup_id,
                subscription_id,
                repo_id,
                run_id,
                thread_target_id,
                lane_id,
                wakeup_kind,
                state,
                available_at,
                claimed_at,
                completed_at,
                reason_text,
                payload_json,
                created_at,
                updated_at,
                dispatched_at,
                timestamp,
                idempotency_key,
                timer_id,
                event_id,
                event_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wakeup_id) DO UPDATE SET
                subscription_id = excluded.subscription_id,
                repo_id = excluded.repo_id,
                run_id = excluded.run_id,
                thread_target_id = excluded.thread_target_id,
                lane_id = excluded.lane_id,
                wakeup_kind = excluded.wakeup_kind,
                state = excluded.state,
                available_at = excluded.available_at,
                claimed_at = excluded.claimed_at,
                completed_at = excluded.completed_at,
                reason_text = excluded.reason_text,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                dispatched_at = excluded.dispatched_at,
                timestamp = excluded.timestamp,
                idempotency_key = excluded.idempotency_key,
                timer_id = excluded.timer_id,
                event_id = excluded.event_id,
                event_type = excluded.event_type
            """,
            (
                entry.get("wakeup_id"),
                entry.get("subscription_id"),
                entry.get("repo_id"),
                entry.get("run_id"),
                entry.get("thread_id"),
                entry.get("lane_id"),
                entry.get("source") or "automation",
                entry.get("state") or "pending",
                entry.get("timestamp"),
                None,
                None,
                entry.get("reason"),
                _json_dumps(payload),
                entry.get("created_at") or now_iso(),
                entry.get("updated_at") or entry.get("created_at") or now_iso(),
                entry.get("dispatched_at"),
                entry.get("timestamp"),
                entry.get("idempotency_key"),
                entry.get("timer_id"),
                entry.get("event_id"),
                entry.get("event_type"),
            ),
        )

    return {
        "subscriptions": len(sub_rows),
        "timers": len(timer_rows),
        "wakeups": len(wakeup_rows),
    }


def backfill_legacy_queue_state(hub_root: Path, conn: Any) -> dict[str, int]:
    queue_dir = hub_root / LEGACY_PMA_QUEUE_DIR
    if not queue_dir.exists():
        return {"lanes": 0, "items": 0}

    lane_count = 0
    item_count = 0
    for path in sorted(queue_dir.glob("*.jsonl")):
        lane_count += 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            item_count += 1
            conn.execute(
                """
                INSERT INTO orch_queue_items (
                    queue_item_id,
                    lane_id,
                    source_kind,
                    source_key,
                    dedupe_key,
                    state,
                    visible_at,
                    claimed_at,
                    completed_at,
                    payload_json,
                    created_at,
                    updated_at,
                    idempotency_key,
                    error_text,
                    dedupe_reason,
                    result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(queue_item_id) DO UPDATE SET
                    lane_id = excluded.lane_id,
                    source_kind = excluded.source_kind,
                    source_key = excluded.source_key,
                    dedupe_key = excluded.dedupe_key,
                    state = excluded.state,
                    visible_at = excluded.visible_at,
                    claimed_at = excluded.claimed_at,
                    completed_at = excluded.completed_at,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    idempotency_key = excluded.idempotency_key,
                    error_text = excluded.error_text,
                    dedupe_reason = excluded.dedupe_reason,
                    result_json = excluded.result_json
                """,
                (
                    entry.get("item_id"),
                    entry.get("lane_id"),
                    "pma_lane",
                    entry.get("item_id"),
                    entry.get("idempotency_key"),
                    entry.get("state") or "pending",
                    entry.get("enqueued_at"),
                    entry.get("started_at"),
                    entry.get("finished_at"),
                    _json_dumps(entry.get("payload") or {}),
                    entry.get("enqueued_at") or now_iso(),
                    entry.get("finished_at")
                    or entry.get("started_at")
                    or entry.get("enqueued_at")
                    or now_iso(),
                    entry.get("idempotency_key"),
                    entry.get("error"),
                    entry.get("dedupe_reason"),
                    _json_dumps(entry.get("result") or {}),
                ),
            )

    return {"lanes": lane_count, "items": item_count}


def backfill_legacy_reactive_state(hub_root: Path, conn: Any) -> dict[str, int]:
    state = _load_json_file(hub_root / LEGACY_PMA_REACTIVE_PATH)
    if state is None:
        return {"keys": 0}
    last_enqueued = state.get("last_enqueued")
    if not isinstance(last_enqueued, dict):
        return {"keys": 0}

    count = 0
    stamp = now_iso()
    for key, value in last_enqueued.items():
        if not isinstance(key, str):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        conn.execute(
            """
            INSERT INTO orch_reactive_debounce_state (
                debounce_key,
                repo_id,
                thread_target_id,
                fingerprint,
                available_at,
                last_event_id,
                metadata_json,
                created_at,
                updated_at,
                last_enqueued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(debounce_key) DO UPDATE SET
                available_at = excluded.available_at,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at,
                last_enqueued_at = excluded.last_enqueued_at
            """,
            (
                key,
                None,
                None,
                None,
                None,
                None,
                "{}",
                stamp,
                stamp,
                parsed,
            ),
        )
        count += 1
    return {"keys": count}


__all__ = [
    "LEGACY_PMA_AUTOMATION_PATH",
    "LEGACY_PMA_QUEUE_DIR",
    "LEGACY_PMA_REACTIVE_PATH",
    "LEGACY_PMA_THREADS_DB_PATH",
    "backfill_legacy_automation_state",
    "backfill_legacy_queue_state",
    "backfill_legacy_reactive_state",
    "backfill_legacy_thread_state",
]
