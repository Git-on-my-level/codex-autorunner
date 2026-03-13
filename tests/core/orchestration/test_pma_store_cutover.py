from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from codex_autorunner.core.pma_reactive import PmaReactiveStore


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
