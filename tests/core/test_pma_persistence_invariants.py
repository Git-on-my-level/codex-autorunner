"""Characterization tests that lock PMA canonical-versus-mirror ownership.

These tests verify that:
- Orchestration SQLite tables remain the canonical state owners.
- JSON/JSONL/legacy-SQLite mirrors are convenience artifacts that can be
  deleted without affecting reload or correctness.
- Thread-store and PmaQueue share `orch_queue_items` with compatible row shapes.
- The automation store's full-table rewrite behavior is characterized.

These tests are behavior-preserving guards for the block-030 refactoring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_automation_store import PmaAutomationStore
from codex_autorunner.core.pma_queue import PmaQueue
from codex_autorunner.core.pma_reactive import PmaReactiveStore
from codex_autorunner.core.pma_thread_store import PmaThreadStore


def _automation_json_mirror_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / "automation_store.json"


def _reactive_json_mirror_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / "reactive_state.json"


def _queue_jsonl_mirror_path(hub_root: Path, lane_id: str) -> Path:
    safe_lane_id = lane_id.replace(":", "__COLON__").replace("/", "__SLASH__")
    return hub_root / ".codex-autorunner" / "pma" / "queue" / f"{safe_lane_id}.jsonl"


def _legacy_thread_db_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / "threads.sqlite3"


def _create_thread(hub_root: Path) -> str:
    store = PmaThreadStore(hub_root)
    thread = store.create_thread("codex", hub_root)
    return str(thread["managed_thread_id"])


# ---------------------------------------------------------------------------
# Automation store: canonical SQLite vs JSON mirror
# ---------------------------------------------------------------------------


class TestAutomationCanonicalInvariants:
    def test_create_subscription_writes_to_sqlite_tables(self, tmp_path: Path) -> None:
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
        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT subscription_id, state FROM orch_automation_subscriptions"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "active"

    def test_subscription_survives_json_mirror_deletion(self, tmp_path: Path) -> None:
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
        mirror_path = _automation_json_mirror_path(hub_root)
        assert mirror_path.exists(), "expected JSON mirror to be written"
        mirror_path.unlink()
        reloaded = PmaAutomationStore(hub_root)
        subs = reloaded.list_subscriptions(state="active")
        assert len(subs) == 1

    def test_create_timer_writes_to_sqlite(self, tmp_path: Path) -> None:
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
        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT timer_id, state FROM orch_automation_timers"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "pending"

    def test_timer_survives_json_mirror_deletion(self, tmp_path: Path) -> None:
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
        _automation_json_mirror_path(hub_root).unlink()
        reloaded = PmaAutomationStore(hub_root)
        timers = reloaded.list_timers(state="pending")
        assert len(timers) == 1

    def test_wakeup_writes_to_sqlite(self, tmp_path: Path) -> None:
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
        store.enqueue_wakeup(
            subscription_id=sub_id,
            lane_id="pma:default",
            source="timer",
        )
        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT wakeup_id, state FROM orch_automation_wakeups"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "pending"

    def test_wakeup_survives_json_mirror_deletion(self, tmp_path: Path) -> None:
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
        store.enqueue_wakeup(
            subscription_id=sub_id,
            lane_id="pma:default",
            source="timer",
        )
        _automation_json_mirror_path(hub_root).unlink()
        reloaded = PmaAutomationStore(hub_root)
        wakeups = reloaded.list_pending_wakeups()
        assert len(wakeups) == 1

    def test_save_does_full_table_rewrite(self, tmp_path: Path) -> None:
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

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sub_count_before = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_subscriptions"
            ).fetchone()["c"]
            timer_count_before = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_timers"
            ).fetchone()["c"]

        store.cancel_subscription(sub_id)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sub_count_after = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_subscriptions"
            ).fetchone()["c"]
            timer_count_after = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_timers"
            ).fetchone()["c"]

        assert sub_count_before == 1
        assert timer_count_before == 1
        assert sub_count_after == 1
        assert timer_count_after == 1

        reloaded = PmaAutomationStore(hub_root)
        all_subs = reloaded.list_subscriptions(include_inactive=True)
        matched = [s for s in all_subs if s["subscription_id"] == sub_id]
        assert len(matched) == 1
        assert matched[0]["state"] == "cancelled"

    def test_json_mirror_is_written_after_each_mutation(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaAutomationStore(hub_root)
        thread_id = _create_thread(hub_root)
        mirror_path = _automation_json_mirror_path(hub_root)

        store.create_subscription(
            thread_id=thread_id,
            event_types=["lifecycle"],
            from_state="running",
            to_state="completed",
            lane_id="pma:default",
        )
        assert mirror_path.exists()
        raw = json.loads(mirror_path.read_text())
        assert len(raw["subscriptions"]) == 1

        sub_id = store.list_subscriptions(state="active")[0]["subscription_id"]
        store.cancel_subscription(sub_id)
        raw2 = json.loads(mirror_path.read_text())
        assert raw2["subscriptions"][0]["state"] == "cancelled"

    def test_cancel_then_purge_subscription_removes_rows(self, tmp_path: Path) -> None:
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
        store.purge_subscription(sub_id)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sub_count = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_subscriptions"
            ).fetchone()["c"]

        assert sub_count == 0

    def test_purge_drops_orphaned_timer_rows(self, tmp_path: Path) -> None:
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
        store.purge_subscription(sub_id)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            timer_count = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_automation_timers"
            ).fetchone()["c"]

        assert timer_count == 0, "orphaned timer rows dropped on full-table rewrite"


# ---------------------------------------------------------------------------
# Queue: canonical SQLite vs JSONL mirror
# ---------------------------------------------------------------------------


class TestQueueCanonicalInvariants:
    @pytest.mark.anyio
    async def test_enqueue_writes_to_orch_queue_items(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item, reason = queue.enqueue_sync("pma:default", "key-1", {"msg": "hello"})
        assert reason is None
        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT queue_item_id, lane_id, state FROM orch_queue_items WHERE queue_item_id = ?",
                (item.item_id,),
            ).fetchone()
        assert row is not None
        assert row["lane_id"] == "pma:default"
        assert row["state"] == "pending"

    @pytest.mark.anyio
    async def test_enqueue_produces_jsonl_mirror(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item, _ = queue.enqueue_sync("pma:default", "key-1", {"msg": "hello"})
        mirror_path = _queue_jsonl_mirror_path(hub_root, "pma:default")
        assert mirror_path.exists()
        lines = mirror_path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["item_id"] == item.item_id

    @pytest.mark.anyio
    async def test_state_survives_jsonl_mirror_deletion(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item, _ = queue.enqueue_sync("pma:default", "key-1", {"msg": "hello"})
        mirror_path = _queue_jsonl_mirror_path(hub_root, "pma:default")
        assert mirror_path.exists()
        mirror_path.unlink()

        replayed = await queue.replay_pending("pma:default")
        assert replayed == 1
        dequeued = await queue.dequeue("pma:default")
        assert dequeued is not None
        assert dequeued.item_id == item.item_id

    @pytest.mark.anyio
    async def test_complete_updates_sqlite_and_rewrites_mirror(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item, _ = queue.enqueue_sync("pma:default", "key-1", {"msg": "hello"})
        await queue.replay_pending("pma:default")
        dequeued = await queue.dequeue("pma:default")
        assert dequeued is not None
        await queue.complete_item(dequeued, {"status": "ok"})

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT state, result_json FROM orch_queue_items WHERE queue_item_id = ?",
                (item.item_id,),
            ).fetchone()
        assert row["state"] == "completed"
        assert json.loads(str(row["result_json"])) == {"status": "ok"}

        mirror_path = _queue_jsonl_mirror_path(hub_root, "pma:default")
        assert mirror_path.exists()
        lines = mirror_path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["state"] == "completed"

    @pytest.mark.anyio
    async def test_fail_updates_sqlite(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item, _ = queue.enqueue_sync("pma:default", "key-1", {"msg": "hello"})
        await queue.replay_pending("pma:default")
        dequeued = await queue.dequeue("pma:default")
        assert dequeued is not None
        await queue.fail_item(dequeued, "something broke")

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT state, error_text FROM orch_queue_items WHERE queue_item_id = ?",
                (item.item_id,),
            ).fetchone()
        assert row["state"] == "failed"
        assert row["error_text"] == "something broke"

    @pytest.mark.anyio
    async def test_mirror_rewritten_after_deletion_and_mutation(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item, _ = queue.enqueue_sync("pma:default", "key-1", {"msg": "hello"})
        mirror_path = _queue_jsonl_mirror_path(hub_root, "pma:default")
        mirror_path.unlink()
        await queue.replay_pending("pma:default")
        dequeued = await queue.dequeue("pma:default")
        assert dequeued is not None
        await queue.complete_item(dequeued, {"status": "ok"})
        assert mirror_path.exists()
        lines = mirror_path.read_text().strip().splitlines()
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Thread-store: queue-row shape for managed turns
# ---------------------------------------------------------------------------


class TestThreadStoreQueueRowShape:
    def test_create_turn_with_running_status_no_queue_row(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaThreadStore(hub_root)
        thread = store.create_thread("codex", hub_root)
        thread_id = str(thread["managed_thread_id"])
        store.create_turn(thread_id, prompt="hello")

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT * FROM orch_queue_items WHERE source_kind = 'thread_execution'"
            ).fetchall()
        assert len(rows) == 0

    def test_create_queued_turn_produces_queue_row(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaThreadStore(hub_root)
        thread = store.create_thread("codex", hub_root)
        thread_id = str(thread["managed_thread_id"])
        store.create_turn(thread_id, prompt="first")
        turn = store.create_turn(thread_id, prompt="second", busy_policy="queue")

        turn_id = str(turn["managed_turn_id"])
        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                """
                SELECT queue_item_id, lane_id, source_kind, source_key, state
                  FROM orch_queue_items
                 WHERE source_kind = 'thread_execution'
                   AND source_key = ?
                """,
                (turn_id,),
            ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["lane_id"] == f"thread:{thread_id}"
        assert row["source_kind"] == "thread_execution"
        assert row["source_key"] == turn_id
        assert row["state"] == "queued"

    def test_mark_turn_finished_completes_running_turn(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaThreadStore(hub_root)
        thread = store.create_thread("codex", hub_root)
        thread_id = str(thread["managed_thread_id"])
        turn = store.create_turn(thread_id, prompt="hello")
        turn_id = str(turn["managed_turn_id"])

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row_before = conn.execute(
                "SELECT state FROM orch_queue_items WHERE source_key = ? AND source_kind = 'thread_execution'",
                (turn_id,),
            ).fetchone()
        assert row_before is None, "running turns do not create queue rows"

        store.mark_turn_finished(turn_id, status="ok")

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            exec_row = conn.execute(
                "SELECT status FROM orch_thread_executions WHERE execution_id = ?",
                (turn_id,),
            ).fetchone()
        assert exec_row["status"] == "ok"

    def test_claim_queued_turn_promotes_queue_row(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaThreadStore(hub_root)
        thread = store.create_thread("codex", hub_root)
        thread_id = str(thread["managed_thread_id"])
        turn1 = store.create_turn(thread_id, prompt="first")
        turn2 = store.create_turn(thread_id, prompt="second", busy_policy="queue")
        turn2_id = str(turn2["managed_turn_id"])

        turn1_id = str(turn1["managed_turn_id"])
        store.mark_turn_finished(turn1_id, status="ok")

        claimed = store.claim_next_queued_turn(thread_id)
        assert claimed is not None
        turn_record, _queue_record = claimed
        assert turn_record["managed_turn_id"] == turn2_id

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            exec_row = conn.execute(
                "SELECT status FROM orch_thread_executions WHERE execution_id = ?",
                (turn2_id,),
            ).fetchone()
            queue_row = conn.execute(
                "SELECT state FROM orch_queue_items WHERE source_key = ?",
                (turn2_id,),
            ).fetchone()
        assert exec_row["status"] == "running"
        assert queue_row["state"] == "running"

    def test_mark_turn_interrupted_updates_queue_row(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaThreadStore(hub_root)
        thread = store.create_thread("codex", hub_root)
        thread_id = str(thread["managed_thread_id"])
        turn1 = store.create_turn(thread_id, prompt="first")
        turn2 = store.create_turn(thread_id, prompt="second", busy_policy="queue")
        turn2_id = str(turn2["managed_turn_id"])
        turn1_id = str(turn1["managed_turn_id"])
        store.mark_turn_finished(turn1_id, status="ok")
        store.claim_next_queued_turn(thread_id)

        store.mark_turn_interrupted(turn2_id)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT state, error_text FROM orch_queue_items WHERE source_key = ?",
                (turn2_id,),
            ).fetchone()
        assert row is not None
        assert row["state"] == "failed"
        assert row["error_text"] is not None and "interrupted" in str(row["error_text"])

    def test_finished_queued_turn_updates_queue_result_json(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        store = PmaThreadStore(hub_root)
        thread = store.create_thread("codex", hub_root)
        thread_id = str(thread["managed_thread_id"])
        turn1 = store.create_turn(thread_id, prompt="first")
        turn2 = store.create_turn(thread_id, prompt="second", busy_policy="queue")
        turn2_id = str(turn2["managed_turn_id"])
        turn1_id = str(turn1["managed_turn_id"])
        store.mark_turn_finished(turn1_id, status="ok")
        store.claim_next_queued_turn(thread_id)

        store.mark_turn_finished(
            turn2_id,
            status="ok",
            assistant_text="world",
            backend_turn_id="bt-1",
        )

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT state, result_json FROM orch_queue_items WHERE source_key = ?",
                (turn2_id,),
            ).fetchone()
        assert row is not None
        assert row["state"] == "completed"
        result = json.loads(str(row["result_json"]))
        assert result["status"] == "ok"
        assert result["backend_turn_id"] == "bt-1"


# ---------------------------------------------------------------------------
# Reactive debounce: canonical SQLite vs JSON mirror
# ---------------------------------------------------------------------------


class TestReactiveCanonicalInvariants:
    def test_check_and_update_writes_to_sqlite(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("repo-1:event-1", 30)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT debounce_key, last_enqueued_at FROM orch_reactive_debounce_state"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["debounce_key"] == "repo-1:event-1"
        assert float(rows[0]["last_enqueued_at"]) > 0

    def test_check_and_update_produces_json_mirror(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("repo-1:event-1", 30)
        mirror_path = _reactive_json_mirror_path(hub_root)
        assert mirror_path.exists()
        raw = json.loads(mirror_path.read_text())
        assert "repo-1:event-1" in raw["last_enqueued"]

    def test_state_survives_json_mirror_deletion(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("repo-1:event-1", 30)
        mirror_path = _reactive_json_mirror_path(hub_root)
        assert mirror_path.exists()
        mirror_path.unlink()

        reloaded = PmaReactiveStore(hub_root)
        assert reloaded.check_and_update("repo-1:event-1", 30) is False

    def test_multiple_keys_are_stored_independently(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("repo-1:a", 30)
        store.check_and_update("repo-1:b", 30)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT debounce_key FROM orch_reactive_debounce_state ORDER BY debounce_key"
            ).fetchall()
        keys = [r["debounce_key"] for r in rows]
        assert "repo-1:a" in keys
        assert "repo-1:b" in keys

        mirror_path = _reactive_json_mirror_path(hub_root)
        raw = json.loads(mirror_path.read_text())
        assert "repo-1:a" in raw["last_enqueued"]
        assert "repo-1:b" in raw["last_enqueued"]

    def test_load_returns_default_when_sqlite_empty(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        state = store.load()
        assert state["version"] == 1
        assert isinstance(state["last_enqueued"], dict)

    def test_save_does_full_table_rewrite(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("repo-1:a", 30)
        store.check_and_update("repo-1:b", 30)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            count_before = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_reactive_debounce_state"
            ).fetchone()["c"]
        assert count_before == 2

        store.check_and_update("repo-1:c", 30)
        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            count_after = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_reactive_debounce_state"
            ).fetchone()["c"]
        assert count_after == 3


# ---------------------------------------------------------------------------
# Legacy thread mirror: gated by CAR_LEGACY_MIRROR_ENABLED
# ---------------------------------------------------------------------------


class TestLegacyThreadMirrorInvariant:
    def test_legacy_mirror_written_when_enabled(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        os.environ["CAR_LEGACY_MIRROR_ENABLED"] = "true"
        try:
            store = PmaThreadStore(hub_root)
            thread = store.create_thread("codex", hub_root)
            thread_id = str(thread["managed_thread_id"])
            store.create_turn(thread_id, prompt="hello")

            legacy_path = _legacy_thread_db_path(hub_root)
            assert legacy_path.exists()

            from codex_autorunner.core.sqlite_utils import open_sqlite

            with open_sqlite(legacy_path, durable=False) as conn:
                threads = conn.execute(
                    "SELECT managed_thread_id FROM pma_managed_threads"
                ).fetchall()
                turns = conn.execute(
                    "SELECT managed_turn_id FROM pma_managed_turns"
                ).fetchall()
            assert len(threads) == 1
            assert len(turns) == 1
        finally:
            os.environ.pop("CAR_LEGACY_MIRROR_ENABLED", None)

    def test_legacy_mirror_not_written_when_disabled(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        os.environ["CAR_LEGACY_MIRROR_ENABLED"] = "false"
        try:
            store = PmaThreadStore(hub_root)
            thread = store.create_thread("codex", hub_root)
            thread_id = str(thread["managed_thread_id"])
            store.create_turn(thread_id, prompt="hello")

            legacy_path = _legacy_thread_db_path(hub_root)
            assert not legacy_path.exists()

            with open_orchestration_sqlite(hub_root, durable=False) as conn:
                row = conn.execute(
                    "SELECT thread_target_id FROM orch_thread_targets"
                ).fetchone()
            assert row is not None
        finally:
            os.environ.pop("CAR_LEGACY_MIRROR_ENABLED", None)

    def test_canonical_state_intact_when_legacy_mirror_disabled(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        os.environ["CAR_LEGACY_MIRROR_ENABLED"] = "false"
        try:
            store = PmaThreadStore(hub_root)
            thread = store.create_thread("codex", hub_root)
            thread_id = str(thread["managed_thread_id"])
            store.create_turn(thread_id, prompt="hello")
            turn_id = str(store.list_turns(thread_id)[0]["managed_turn_id"])
            store.mark_turn_finished(turn_id, status="ok")

            with open_orchestration_sqlite(hub_root, durable=False) as conn:
                t_row = conn.execute(
                    "SELECT lifecycle_status FROM orch_thread_targets WHERE thread_target_id = ?",
                    (thread_id,),
                ).fetchone()
                e_row = conn.execute(
                    "SELECT status FROM orch_thread_executions WHERE execution_id = ?",
                    (turn_id,),
                ).fetchone()
            assert t_row["lifecycle_status"] is not None
            assert e_row["status"] == "ok"
        finally:
            os.environ.pop("CAR_LEGACY_MIRROR_ENABLED", None)


# ---------------------------------------------------------------------------
# Cross-cutting: mirror-file presence after each mutation type
# ---------------------------------------------------------------------------


class TestMirrorFileSyncInvariants:
    def test_automation_mirror_created_on_first_subscription(
        self, tmp_path: Path
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
        mirror = _automation_json_mirror_path(hub_root)
        assert mirror.exists()
        data = json.loads(mirror.read_text())
        assert "subscriptions" in data
        assert len(data["subscriptions"]) == 1

    def test_queue_mirror_created_on_enqueue(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        queue.enqueue_sync("lane-1", "k1", {"v": 1})
        mirror_path = _queue_jsonl_mirror_path(hub_root, "lane-1")
        assert mirror_path.exists()
        lines = mirror_path.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_reactive_mirror_deleted_and_regenerated(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("key-a", 10)
        mirror = _reactive_json_mirror_path(hub_root)
        assert mirror.exists()
        mirror.unlink()
        assert not mirror.exists()
        store.check_and_update("key-b", 10)
        assert mirror.exists()
        raw = json.loads(mirror.read_text())
        assert "key-a" in raw["last_enqueued"]
        assert "key-b" in raw["last_enqueued"]

    @pytest.mark.xfail(
        reason="PmaThreadStore reactive write does not degrade on OSError", strict=True
    )
    def test_reactive_canonical_survives_mirror_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub_root = tmp_path / "hub"
        store = PmaReactiveStore(hub_root)
        store.check_and_update("key-1", 10)

        import codex_autorunner.core.pma_reactive as _mod

        def _failing_atomic_write(path, content):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(_mod, "atomic_write", _failing_atomic_write)

        assert store.check_and_update("key-2", 10) is True

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT debounce_key FROM orch_reactive_debounce_state ORDER BY debounce_key"
            ).fetchall()
        keys = [r["debounce_key"] for r in rows]
        assert "key-1" in keys
        assert "key-2" in keys

        mirror = _reactive_json_mirror_path(hub_root)
        assert mirror.exists()
        raw = json.loads(mirror.read_text())
        assert "key-1" in raw["last_enqueued"]
        assert "key-2" not in raw["last_enqueued"]


class TestAutomationLoadFallbackBehavior:
    def test_load_reads_from_sqlite_first(self, tmp_path: Path) -> None:
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

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sub_rows = conn.execute(
                "SELECT subscription_id FROM orch_automation_subscriptions"
            ).fetchall()
        assert len(sub_rows) == 1

        reloaded = PmaAutomationStore(hub_root)
        state = reloaded.load()
        assert len(state["subscriptions"]) == 1

    def test_load_produces_default_state_when_both_sqlite_and_mirror_empty(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        store = PmaAutomationStore(hub_root)
        state = store.load()
        assert state["version"] == 1
        assert isinstance(state["subscriptions"], list)
        assert isinstance(state["timers"], list)
        assert isinstance(state["wakeups"], list)

    def test_load_falls_back_to_json_mirror_when_sqlite_tables_absent(
        self, tmp_path: Path
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

        mirror_path = _automation_json_mirror_path(hub_root)
        assert mirror_path.exists()

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            conn.execute("DROP TABLE IF EXISTS orch_automation_subscriptions")
            conn.execute("DROP TABLE IF EXISTS orch_automation_timers")
            conn.execute("DROP TABLE IF EXISTS orch_automation_wakeups")
            conn.commit()

        reloaded = PmaAutomationStore(hub_root)
        state = reloaded.load()
        assert len(state["subscriptions"]) == 1


class TestAutomationSaveFullTableRewriteCharacterization:
    def test_save_deletes_and_reinserts_all_rows(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        store = PmaAutomationStore(hub_root)
        thread_a = _create_thread(hub_root)
        store.create_subscription(
            thread_id=thread_a,
            event_types=["lifecycle"],
            from_state="running",
            to_state="completed",
            lane_id="pma:default",
        )

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sub_ids_before = {
                r["subscription_id"]
                for r in conn.execute(
                    "SELECT subscription_id FROM orch_automation_subscriptions"
                ).fetchall()
            }

        store.create_subscription(
            thread_id=thread_a,
            event_types=["lifecycle"],
            from_state="running",
            to_state="failed",
            lane_id="pma:default",
        )

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            sub_ids_after = {
                r["subscription_id"]
                for r in conn.execute(
                    "SELECT subscription_id FROM orch_automation_subscriptions"
                ).fetchall()
            }

        assert len(sub_ids_before) == 1
        assert len(sub_ids_after) == 2

    def test_save_drops_orphaned_wakeups_during_rewrite(self, tmp_path: Path) -> None:
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
        store.enqueue_wakeup(
            subscription_id=sub_id,
            lane_id="pma:default",
            source="timer",
        )

        store.cancel_subscription(sub_id)
        store.purge_subscription(sub_id)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT wakeup_id FROM orch_automation_wakeups"
            ).fetchall()
        assert len(rows) == 0


class TestThreadStoreCanonicalVsRuntimeBinding:
    def test_backend_thread_id_stored_in_orchestration_row(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        store.create_thread("codex", workspace, backend_thread_id="backend-1")

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT backend_thread_id FROM orch_thread_targets"
            ).fetchone()
        assert row is not None
        assert row["backend_thread_id"] == "backend-1"

    def test_runtime_binding_is_separate_from_orchestration_row(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread(
            "codex",
            workspace,
            backend_thread_id="backend-1",
            metadata={"backend_runtime_instance_id": "runtime-1"},
        )

        fetched = store.get_thread(created["managed_thread_id"])
        assert fetched is not None
        assert "backend_thread_id" not in fetched
        assert "backend_runtime_instance_id" not in fetched

        binding = store.get_thread_runtime_binding(created["managed_thread_id"])
        assert binding is not None
        assert binding.backend_thread_id == "backend-1"
        assert binding.backend_runtime_instance_id == "runtime-1"

    def test_canonical_state_survives_legacy_mirror_deletion(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread("codex", workspace)
        thread_id = str(created["managed_thread_id"])
        store.create_turn(thread_id, prompt="hello")
        turn_id = str(store.list_turns(thread_id)[0]["managed_turn_id"])

        legacy_path = _legacy_thread_db_path(hub_root)
        if legacy_path.exists():
            legacy_path.unlink()

        restarted = PmaThreadStore(hub_root)
        fetched = restarted.get_thread(thread_id)
        assert fetched is not None
        assert fetched["managed_thread_id"] == thread_id

        turns = restarted.list_turns(thread_id)
        assert len(turns) == 1
        assert turns[0]["managed_turn_id"] == turn_id

    def test_set_thread_backend_id_updates_orchestration_row(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread("codex", workspace)
        thread_id = created["managed_thread_id"]

        store.set_thread_backend_id(thread_id, "backend-new")

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                "SELECT backend_thread_id FROM orch_thread_targets WHERE thread_target_id = ?",
                (thread_id,),
            ).fetchone()
        assert row is not None
        assert row["backend_thread_id"] == "backend-new"

        binding = store.get_thread_runtime_binding(thread_id)
        assert binding is not None
        assert binding.backend_thread_id == "backend-new"


class TestQueueMirrorRewriteCharacterization:
    @pytest.mark.anyio
    async def test_mirror_contains_all_states_after_mixed_operations(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        item1, _ = queue.enqueue_sync("pma:default", "k1", {"v": 1})
        item2, _ = queue.enqueue_sync("pma:default", "k2", {"v": 2})
        await queue.replay_pending("pma:default")

        dequeued1 = await queue.dequeue("pma:default")
        assert dequeued1 is not None
        await queue.complete_item(dequeued1, {"status": "ok"})

        dequeued2 = await queue.dequeue("pma:default")
        assert dequeued2 is not None
        await queue.fail_item(dequeued2, "broke")

        mirror_path = _queue_jsonl_mirror_path(hub_root, "pma:default")
        assert mirror_path.exists()
        lines = mirror_path.read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        states = {p["item_id"]: p["state"] for p in parsed}
        assert states[item1.item_id] == "completed"
        assert states[item2.item_id] == "failed"

    @pytest.mark.anyio
    async def test_compaction_removes_old_terminal_items_but_keeps_recent(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        queue = PmaQueue(hub_root)
        items = []
        for i in range(5):
            item, _ = queue.enqueue_sync("pma:compact", f"k{i}", {"v": i})
            items.append(item)
        await queue.replay_pending("pma:compact")

        for _item in items:
            dequeued = await queue.dequeue("pma:compact")
            assert dequeued is not None
            await queue.complete_item(dequeued, {"status": "ok"})

        result = await queue.compact_lane("pma:compact", keep_last=2)
        assert result is True

        all_items = await queue.list_items("pma:compact")
        assert len(all_items) == 2
        remaining_ids = {i.item_id for i in all_items}
        assert items[-1].item_id in remaining_ids
        assert items[-2].item_id in remaining_ids


class TestThreadStoreTurnLifecycleCanonicalInvariants:
    def test_mark_turn_finished_returns_false_for_already_interrupted(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread("codex", workspace)
        thread_id = created["managed_thread_id"]
        turn = store.create_turn(thread_id, prompt="hello")
        turn_id = turn["managed_turn_id"]

        assert store.mark_turn_interrupted(turn_id) is True
        assert store.mark_turn_finished(turn_id, status="ok") is False

        fetched = store.get_turn(thread_id, turn_id)
        assert fetched is not None
        assert fetched["status"] == "interrupted"
        assert fetched["assistant_text"] is None

    def test_duplicate_mark_turn_finished_is_idempotent(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread("codex", workspace)
        thread_id = created["managed_thread_id"]
        turn = store.create_turn(thread_id, prompt="hello")
        turn_id = turn["managed_turn_id"]

        assert store.mark_turn_finished(turn_id, status="ok") is True
        assert store.mark_turn_finished(turn_id, status="ok") is False

    def test_mark_turn_interrupted_on_running_turn_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread("codex", workspace)
        thread_id = created["managed_thread_id"]
        turn = store.create_turn(thread_id, prompt="hello")
        turn_id = turn["managed_turn_id"]

        assert store.mark_turn_interrupted(turn_id) is True
        assert store.mark_turn_interrupted(turn_id) is False

    def test_finished_turn_does_not_create_queue_row(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = PmaThreadStore(hub_root)
        created = store.create_thread("codex", workspace)
        thread_id = created["managed_thread_id"]
        turn = store.create_turn(thread_id, prompt="hello")
        turn_id = turn["managed_turn_id"]

        store.mark_turn_finished(turn_id, status="ok")

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            rows = conn.execute(
                "SELECT * FROM orch_queue_items WHERE source_kind = 'thread_execution' AND source_key = ?",
                (turn_id,),
            ).fetchall()
        assert len(rows) == 0


class TestAutomationNotifyTransitionCharacterization:
    def test_notify_transition_matches_subscription_and_creates_wakeup(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        store = PmaAutomationStore(hub_root)
        thread_id = _create_thread(hub_root)
        store.create_subscription(
            {
                "event_type": "flow_completed",
                "thread_id": thread_id,
                "from_state": "running",
                "to_state": "completed",
                "lane_id": "pma:default",
            }
        )

        result = store.notify_transition(
            {
                "event_type": "flow_completed",
                "thread_id": thread_id,
                "from_state": "running",
                "to_state": "completed",
                "transition_id": "trans-1",
            }
        )
        assert result["matched"] == 1
        assert result["created"] == 1

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            wakeups = conn.execute(
                "SELECT wakeup_id, state FROM orch_automation_wakeups"
            ).fetchall()
        assert len(wakeups) == 1
        assert wakeups[0]["state"] == "pending"

    def test_notify_transition_with_no_matching_subscription_creates_no_wakeup(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        store = PmaAutomationStore(hub_root)

        result = store.notify_transition(
            {
                "event_type": "flow_completed",
                "from_state": "running",
                "to_state": "completed",
                "transition_id": "trans-1",
            }
        )
        assert result["matched"] == 0
        assert result["created"] == 0

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            wakeups = conn.execute(
                "SELECT wakeup_id FROM orch_automation_wakeups"
            ).fetchall()
        assert len(wakeups) == 0


class TestPmaStateStoreIsRuntimeOnly:
    def test_pma_state_store_does_not_touch_orchestration_tables(
        self, tmp_path: Path
    ) -> None:
        from codex_autorunner.core.pma_state import PmaStateStore

        hub_root = tmp_path / "hub"
        store = PmaStateStore(hub_root)
        store.save(
            {
                "version": 1,
                "active": True,
                "current": {"turn_id": "t-1"},
                "last_result": {},
                "updated_at": "2025-01-01T00:00:00Z",
            }
        )
        state = store.load()
        assert state["active"] is True
        assert state["current"]["turn_id"] == "t-1"

        db_path = hub_root / ".codex-autorunner" / "orchestration.sqlite3"
        if db_path.exists():
            with open_orchestration_sqlite(hub_root, durable=False) as conn:
                tables = {
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            reactive_rows = 0
            if "orch_reactive_debounce_state" in tables:
                reactive_rows = conn.execute(
                    "SELECT COUNT(*) AS c FROM orch_reactive_debounce_state"
                ).fetchone()["c"]
            assert reactive_rows == 0
