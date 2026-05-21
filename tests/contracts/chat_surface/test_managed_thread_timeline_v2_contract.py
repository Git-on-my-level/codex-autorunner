from __future__ import annotations

from pathlib import Path
from typing import Any

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import initialize_orchestration_sqlite
from codex_autorunner.core.orchestration.managed_thread_timeline import (
    build_managed_thread_timeline,
    timeline_item_from_tail_event,
)
from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    OutputDelta,
    RunNotice,
    ToolCall,
    ToolResult,
)
from tests.support.turn_execution import create_test_turn


def _store(tmp_path: Path) -> tuple[Path, ManagedThreadStore, str]:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    workspace = hub_root / "worktree"
    workspace.mkdir(parents=True)
    store = ManagedThreadStore(hub_root)
    thread = store.create_thread("codex", workspace)
    return hub_root, store, str(thread["managed_thread_id"])


def _by_kind(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [item for item in items if item["kind"] == kind]


def _assert_identity(item: dict[str, Any]) -> None:
    assert item["contract_version"] == "managed_thread_timeline.v3"
    assert item["identity"]["timeline_item_id"] == item["item_id"]
    assert "progress_item_ids" in item["identity"]
    assert "source_event_ids" in item["provenance"]
    assert "progress_event_ids" in item["provenance"]


def test_managed_thread_timeline_v2_authors_identity_and_provenance_for_core_items(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(store, thread_id, prompt="run checks")
    turn_id = str(turn["managed_turn_id"])
    persist_turn_timeline(
        hub_root,
        execution_id=turn_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=[
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
                tool_input={"path": "tests/contracts"},
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
        ],
    )
    assert store.mark_turn_finished(turn_id, status="ok", assistant_text="done")

    timeline = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )
    assert timeline["contract_version"] == "managed_thread_timeline.v3"
    for item in timeline["items"]:
        _assert_identity(item)

    user = _by_kind(timeline["items"], "user_message")[0]
    assert user["item_id"] == f"turn:{turn_id}:user"
    assert user["identity"]["progress_item_ids"] == []
    assert user["provenance"]["source_event_ids"] == []
    assert user["provenance"]["progress_event_ids"] == []

    notice = _by_kind(timeline["items"], "intermediate")[0]
    assert notice["payload"]["intermediate_kind"] == "progress"
    assert notice["identity"]["progress_item_ids"] == ["progress:notice:0001"]
    assert notice["provenance"]["source_event_ids"] == [1]
    assert notice["provenance"]["progress_event_ids"] == [1]

    assert all(
        item["payload"].get("source_event_type") != "output_delta"
        for item in timeline["items"]
    )

    tool_group = _by_kind(timeline["items"], "tool_group")[0]
    assert tool_group["identity"]["progress_item_ids"] == [
        "progress:tool:0003:pytest",
        "progress:tool:0004:pytest",
    ]
    assert tool_group["provenance"]["source_event_ids"] == [3, 4]
    assert tool_group["provenance"]["progress_event_ids"] == [3, 4]

    approval = _by_kind(timeline["items"], "approval")[0]
    assert approval["identity"]["progress_item_ids"] == ["progress:approval:write-1"]
    assert approval["provenance"]["source_event_ids"] == [5]
    assert approval["provenance"]["progress_event_ids"] == [5]

    assistant = _by_kind(timeline["items"], "assistant_message")[0]
    assert assistant["provenance"]["source_event_ids"] == [6]
    assert assistant["provenance"]["progress_event_ids"] == [6]

    terminal = _by_kind(timeline["items"], "status")[0]
    assert terminal["item_id"] == f"turn:{turn_id}:status:ok"
    assert terminal["provenance"]["source_event_ids"] == [6]
    assert terminal["provenance"]["progress_event_ids"] == [6]


def test_live_timeline_frame_keeps_sse_cursor_out_of_timeline_identity() -> None:
    item = timeline_item_from_tail_event(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        tail_event={
            "event_id": 99,
            "event_type": "assistant_update",
            "summary": "Reading files",
            "received_at": "2026-05-12T10:00:02Z",
            "progress_item": {
                "item_id": "progress:assistant_update:0002",
                "kind": "assistant_update",
                "state": "running",
                "title": "Thinking",
                "summary": "Reading files",
                "event_ids": [2],
            },
            "progress_item_id": "progress:assistant_update:0002",
            "progress_kind": "assistant_update",
            "progress_state": "running",
        },
    )

    assert item is not None
    assert item["contract_version"] == "managed_thread_timeline.v3"
    assert item["item_id"] == "turn:turn-1:intermediate:0002"
    assert item["identity"]["timeline_item_id"] == item["item_id"]
    assert item["identity"]["timeline_item_id"] != "99"
    assert item["identity"]["progress_item_ids"] == ["progress:assistant_update:0002"]
    assert item["provenance"]["source_event_ids"] == [2]
    assert item["provenance"]["progress_event_ids"] == [2]
    assert item["provenance"]["cursor_event_id"] == "99"
