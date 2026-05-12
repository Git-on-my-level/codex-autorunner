from __future__ import annotations

from pathlib import Path
from typing import Any

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import initialize_orchestration_sqlite
from codex_autorunner.core.orchestration.managed_thread_timeline import (
    build_managed_thread_timeline,
    timeline_item_from_tail_event,
)
from codex_autorunner.core.orchestration.progress_projection import (
    ProgressProjectionState,
)
from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
from codex_autorunner.core.pma.tail_serialization import (
    _runtime_terminal_tail_event,
    _tail_event_from_run_event,
)
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    OutputDelta,
    RunEvent,
    RunNotice,
    ToolCall,
    ToolResult,
)


def _store(tmp_path: Path) -> tuple[Path, ManagedThreadStore, str]:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    workspace = hub_root / "worktree"
    workspace.mkdir(parents=True)
    store = ManagedThreadStore(hub_root)
    thread = store.create_thread("codex", workspace)
    return hub_root, store, str(thread["managed_thread_id"])


def _canonical_fields(item: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(item["provenance"])
    provenance.pop("cursor_event_id", None)
    return {
        "item_id": item["item_id"],
        "identity": item["identity"],
        "provenance": provenance,
    }


def _latest_live_items(
    *,
    thread_id: str,
    turn_id: str,
    events: list[RunEvent],
) -> dict[str, dict[str, Any]]:
    state = ProgressProjectionState()
    live_by_id: dict[str, dict[str, Any]] = {}
    for event_id, event in enumerate(events, start=1):
        if isinstance(event, Completed):
            tail_event = _runtime_terminal_tail_event(
                raw_event={"method": "prompt/completed", "params": {"status": "ok"}},
                event_id=event_id,
                received_at=event.timestamp,
            )
        else:
            tail_event = _tail_event_from_run_event(
                event,
                event_id=event_id,
                received_at=event.timestamp,
                projection_state=state,
            )
        if tail_event is None:
            continue
        item = timeline_item_from_tail_event(
            managed_thread_id=thread_id,
            managed_turn_id=turn_id,
            tail_event=tail_event,
        )
        if item is not None:
            live_by_id[item["item_id"]] = item
    return live_by_id


def test_live_tail_frames_match_durable_timeline_identity_and_provenance(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = store.create_turn(thread_id, prompt="run parity checks")
    turn_id = str(turn["managed_turn_id"])
    events: list[RunEvent] = [
        RunNotice(
            timestamp="2026-05-12T10:00:01Z",
            kind="progress",
            message="Starting pytest",
        ),
        OutputDelta(
            timestamp="2026-05-12T10:00:02Z",
            delta_type="assistant_stream",
            content="Reading files",
        ),
        ToolCall(
            timestamp="2026-05-12T10:00:03Z",
            tool_name="pytest",
            tool_input={"path": "tests/surfaces/web/routes/pma_routes"},
        ),
        ToolResult(
            timestamp="2026-05-12T10:00:04Z",
            tool_name="pytest",
            status="ok",
            result="passed",
        ),
        ApprovalRequested(
            timestamp="2026-05-12T10:00:05Z",
            request_id="write-1",
            description="Allow write",
            context={"scope": "workspace"},
        ),
        Completed(
            timestamp="2026-05-12T10:00:06Z",
            final_message="done",
        ),
    ]
    persist_turn_timeline(
        hub_root,
        execution_id=turn_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=events,
    )
    assert store.mark_turn_finished(turn_id, status="ok", assistant_text="done")

    durable = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )
    durable_by_id = {item["item_id"]: item for item in durable["items"]}
    live_by_id = _latest_live_items(
        thread_id=thread_id,
        turn_id=turn_id,
        events=events,
    )

    expected_item_ids = [
        f"turn:{turn_id}:intermediate:0001",
        f"turn:{turn_id}:intermediate:0002",
        f"turn:{turn_id}:tool:3:pytest",
        f"turn:{turn_id}:approval:write-1",
        f"turn:{turn_id}:status:ok",
    ]
    for item_id in expected_item_ids:
        assert _canonical_fields(live_by_id[item_id]) == _canonical_fields(
            durable_by_id[item_id]
        )
        assert live_by_id[item_id]["provenance"]["cursor_event_id"] is not None
        assert live_by_id[item_id]["provenance"]["cursor_event_id"] != item_id
