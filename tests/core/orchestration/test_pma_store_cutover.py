"""Cutover tests: verify PMA stores work correctly after compatibility mirrors
are removed, confirming that orchestration SQLite is the single source of truth.

These tests complement the broader characterization coverage in
``tests/core/test_pma_persistence_invariants.py`` by focusing on reload and
continued-operation scenarios after mirror deletion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_automation_store import PmaAutomationStore
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from codex_autorunner.core.pma_reactive import PmaReactiveStore
from codex_autorunner.core.pma_thread_store import PmaThreadStore


def _create_thread(hub_root: Path) -> str:
    store = PmaThreadStore(hub_root)
    thread = store.create_thread("codex", hub_root)
    return str(thread["managed_thread_id"])


@pytest.mark.anyio
async def test_queue_cutover_keeps_processing_after_legacy_lane_file_is_removed(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_queue = PmaQueue(hub_root)
    item, reason = legacy_queue.enqueue_sync(
        "pma:default",
        "legacy-key-1",
        {"message": "hello"},
    )
    assert reason is None

    queue = PmaQueue(hub_root)
    legacy_path = queue._lane_queue_path("pma:default")
    assert legacy_path.exists()
    legacy_path.unlink()

    replayed = await queue.replay_pending("pma:default")
    assert replayed == 1
    dequeued = await queue.dequeue("pma:default")
    assert dequeued is not None
    assert dequeued.item_id == item.item_id
    await queue.complete_item(dequeued, {"status": "ok"})

    items = await queue.list_items("pma:default")
    assert len(items) == 1
    assert items[0].state == QueueItemState.COMPLETED
    assert queue._lane_queue_path("pma:default").exists()

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute(
            """
            SELECT state, result_json
              FROM orch_queue_items
             WHERE queue_item_id = ?
            """,
            (item.item_id,),
        ).fetchone()
    assert row is not None
    assert row["state"] == "completed"
    assert json.loads(str(row["result_json"])) == {"status": "ok"}


def test_reactive_cutover_works_after_legacy_state_file_is_removed(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    reactive = PmaReactiveStore(hub_root)
    assert reactive.check_and_update("repo-1:event-1", 30) is True
    legacy_path = hub_root / ".codex-autorunner" / "pma" / "reactive_state.json"
    assert legacy_path.exists()
    legacy_path.unlink()

    reloaded = PmaReactiveStore(hub_root)
    assert reloaded.check_and_update("repo-1:event-1", 30) is False

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute(
            """
            SELECT last_enqueued_at
              FROM orch_reactive_debounce_state
             WHERE debounce_key = 'repo-1:event-1'
            """
        ).fetchone()
    assert row is not None
    assert float(row["last_enqueued_at"]) > 0


def test_automation_cutover_subscription_survives_json_mirror_removal(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    store = PmaAutomationStore(hub_root)
    thread_id = _create_thread(hub_root)
    store.create_subscription(
        thread_id=thread_id,
        event_types=["lifecycle"],
        from_state="running",
        to_state="completed",
        lane_id="pma:default",
    )
    mirror_path = hub_root / ".codex-autorunner" / "pma" / "automation_store.json"
    assert mirror_path.exists()
    mirror_path.unlink()

    reloaded = PmaAutomationStore(hub_root)
    subs = reloaded.list_subscriptions(state="active")
    assert len(subs) == 1
    assert subs[0]["thread_id"] == thread_id

    reloaded.cancel_subscription(subs[0]["subscription_id"])
    all_subs = reloaded.list_subscriptions(include_inactive=True)
    assert any(s["state"] == "cancelled" for s in all_subs)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute(
            "SELECT state FROM orch_automation_subscriptions WHERE subscription_id = ?",
            (subs[0]["subscription_id"],),
        ).fetchone()
    assert row["state"] == "cancelled"


def test_automation_cutover_timer_and_wakeup_survive_mirror_removal(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    store = PmaAutomationStore(hub_root)
    thread_id = _create_thread(hub_root)
    store.create_subscription(
        thread_id=thread_id,
        event_types=["lifecycle"],
        from_state="running",
        to_state="completed",
        lane_id="pma:default",
    )
    sub_id = store.list_subscriptions(state="active")[0]["subscription_id"]
    store.create_timer(
        subscription_id=sub_id,
        timer_type="watchdog",
        due_at_seconds=3600,
        lane_id="pma:default",
    )
    store.enqueue_wakeup(
        subscription_id=sub_id,
        lane_id="pma:default",
        source="timer",
    )
    mirror_path = hub_root / ".codex-autorunner" / "pma" / "automation_store.json"
    assert mirror_path.exists()
    mirror_path.unlink()

    reloaded = PmaAutomationStore(hub_root)
    timers = reloaded.list_timers(state="pending")
    assert len(timers) == 1
    assert timers[0]["subscription_id"] == sub_id
    wakeups = reloaded.list_pending_wakeups()
    assert len(wakeups) == 1

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        timer_count = conn.execute(
            "SELECT COUNT(*) AS c FROM orch_automation_timers"
        ).fetchone()["c"]
        wakeup_count = conn.execute(
            "SELECT COUNT(*) AS c FROM orch_automation_wakeups"
        ).fetchone()["c"]
    assert timer_count == 1
    assert wakeup_count == 1


def test_thread_store_cutover_canonical_rows_without_legacy_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_LEGACY_MIRROR_ENABLED", "false")
    hub_root = tmp_path / "hub"
    store = PmaThreadStore(hub_root)
    thread = store.create_thread("codex", hub_root)
    thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(thread_id, prompt="hello")
    turn_id = str(turn["managed_turn_id"])
    store.mark_turn_finished(turn_id, status="ok")

    legacy_path = hub_root / ".codex-autorunner" / "pma" / "threads.sqlite3"
    assert not legacy_path.exists(), "legacy mirror should not exist when disabled"

    reloaded = PmaThreadStore(hub_root)
    turns = reloaded.list_turns(thread_id)
    assert len(turns) == 1
    assert turns[0]["status"] == "ok"

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        t_row = conn.execute(
            "SELECT thread_target_id, lifecycle_status FROM orch_thread_targets WHERE thread_target_id = ?",
            (thread_id,),
        ).fetchone()
        e_row = conn.execute(
            "SELECT execution_id, status FROM orch_thread_executions WHERE execution_id = ?",
            (turn_id,),
        ).fetchone()
    assert t_row is not None
    assert e_row is not None
    assert e_row["status"] == "ok"


def test_thread_store_cutover_queued_turn_survives_without_legacy_mirror(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    store = PmaThreadStore(hub_root)
    thread = store.create_thread("codex", hub_root)
    thread_id = str(thread["managed_thread_id"])
    turn1 = store.create_turn(thread_id, prompt="first")
    turn2 = store.create_turn(thread_id, prompt="second", busy_policy="queue")
    turn1_id = str(turn1["managed_turn_id"])
    turn2_id = str(turn2["managed_turn_id"])
    store.mark_turn_finished(turn1_id, status="ok")

    reloaded = PmaThreadStore(hub_root)
    claimed = reloaded.claim_next_queued_turn(thread_id)
    assert claimed is not None
    turn_record, _queue_record = claimed
    assert turn_record["managed_turn_id"] == turn2_id

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        q_row = conn.execute(
            "SELECT state FROM orch_queue_items WHERE source_key = ? AND source_kind = 'thread_execution'",
            (turn2_id,),
        ).fetchone()
        e_row = conn.execute(
            "SELECT status FROM orch_thread_executions WHERE execution_id = ?",
            (turn2_id,),
        ).fetchone()
    assert q_row["state"] == "running"
    assert e_row["status"] == "running"


def test_automation_cutover_purge_removes_sqlite_rows_not_just_mirror(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    store = PmaAutomationStore(hub_root)
    thread_id = _create_thread(hub_root)
    store.create_subscription(
        thread_id=thread_id,
        event_types=["lifecycle"],
        from_state="running",
        to_state="completed",
        lane_id="pma:default",
    )
    sub_id = store.list_subscriptions(state="active")[0]["subscription_id"]
    store.create_timer(
        subscription_id=sub_id,
        timer_type="watchdog",
        due_at_seconds=3600,
        lane_id="pma:default",
    )
    store.cancel_subscription(sub_id)

    mirror_path = hub_root / ".codex-autorunner" / "pma" / "automation_store.json"
    assert mirror_path.exists()
    mirror_path.unlink()

    reloaded = PmaAutomationStore(hub_root)
    reloaded.purge_subscription(sub_id)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        sub_count = conn.execute(
            "SELECT COUNT(*) AS c FROM orch_automation_subscriptions"
        ).fetchone()["c"]
        timer_count = conn.execute(
            "SELECT COUNT(*) AS c FROM orch_automation_timers"
        ).fetchone()["c"]
    assert sub_count == 0
    assert timer_count == 0

    mirror_after = hub_root / ".codex-autorunner" / "pma" / "automation_store.json"
    assert mirror_after.exists()
    raw = json.loads(mirror_after.read_text())
    assert len(raw["subscriptions"]) == 0
    assert len(raw["timers"]) == 0


def test_all_mirrors_regenerated_after_full_deletion(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    store = PmaAutomationStore(hub_root)
    thread_id = _create_thread(hub_root)
    store.create_subscription(
        thread_id=thread_id,
        event_types=["lifecycle"],
        from_state="running",
        to_state="completed",
        lane_id="pma:default",
    )
    queue = PmaQueue(hub_root)
    queue.enqueue_sync("pma:default", "k1", {"v": 1})
    reactive = PmaReactiveStore(hub_root)
    reactive.check_and_update("repo-1:event-1", 30)

    pma_dir = hub_root / ".codex-autorunner" / "pma"
    automation_mirror = pma_dir / "automation_store.json"
    reactive_mirror = pma_dir / "reactive_state.json"
    queue_mirror = pma_dir / "queue" / "pma__COLON__default.jsonl"
    assert automation_mirror.exists()
    assert reactive_mirror.exists()
    assert queue_mirror.exists()

    automation_mirror.unlink()
    reactive_mirror.unlink()
    queue_mirror.unlink()

    store2 = PmaAutomationStore(hub_root)
    subs = store2.list_subscriptions(state="active")
    assert len(subs) == 1
    store2.cancel_subscription(subs[0]["subscription_id"])
    assert automation_mirror.exists()

    reactive2 = PmaReactiveStore(hub_root)
    assert reactive2.check_and_update("repo-1:event-2", 30) is True
    assert reactive_mirror.exists()

    queue2 = PmaQueue(hub_root)
    queue2.enqueue_sync("pma:default", "k2", {"v": 2})
    assert queue_mirror.exists()
