from __future__ import annotations

import time

from codex_autorunner.core.orchestration.progress_projection import (
    ProgressProjectionItem,
)
from codex_autorunner.core.pma.tail_serialization import (
    _run_event_from_timeline_entry,
    _serialize_persisted_timeline_tail_events,
    _tail_event_from_progress_item,
    build_live_activity_projection,
)
from codex_autorunner.core.ports.run_event import Interrupted


def test_live_activity_coalesces_only_assistant_updates() -> None:
    projection = build_live_activity_projection(
        snapshot={
            "managed_thread_id": "thread-1",
            "managed_turn_id": "turn-1",
            "activity": "running",
            "events": [
                {
                    "event_id": 1,
                    "event_type": "assistant_update",
                    "summary": "Reading",
                    "progress_event_ids": [1],
                },
                {
                    "event_id": 2,
                    "event_type": "assistant_update",
                    "summary": " files",
                    "progress_event_ids": [2],
                },
                {
                    "event_id": 3,
                    "event_type": "progress",
                    "progress_kind": "approval",
                    "title": "Approval requested",
                    "summary": "Approve command",
                    "progress_item_id": "progress:approval:req-1",
                    "progress_event_ids": [3],
                },
                {
                    "event_id": 4,
                    "event_type": "progress",
                    "progress_kind": "notice",
                    "title": "Still running",
                    "summary": "Waiting",
                    "progress_item_id": "progress:notice:0004",
                    "progress_event_ids": [4],
                },
            ],
        },
        event_window=10,
    )

    events = projection["events"]
    assert len(events) == 3
    assert events[0]["event_type"] == "assistant_update"
    assert events[0]["coalesced_event_count"] == 2
    assert events[1]["progress_kind"] == "approval"
    assert events[1]["summary"] == "Approve command"
    assert events[2]["progress_kind"] == "notice"


def test_turn_interrupted_timeline_entry_deserializes_to_interrupted() -> None:
    event = _run_event_from_timeline_entry(
        {
            "event_type": "turn_interrupted",
            "timestamp": "2026-05-15T00:00:00Z",
            "event": {
                "timestamp": "2026-05-15T00:00:00Z",
                "reason": "Runtime thread interrupted",
            },
        }
    )

    assert isinstance(event, Interrupted)
    assert event.reason == "Runtime thread interrupted"


def test_persisted_tail_serializes_turn_interrupted_as_interrupted_event() -> None:
    events, last_activity_at = _serialize_persisted_timeline_tail_events(
        [
            {
                "event_index": 7,
                "event_type": "turn_interrupted",
                "timestamp": "2026-05-15T00:00:00Z",
                "event": {
                    "timestamp": "2026-05-15T00:00:00Z",
                    "reason": "Runtime thread interrupted",
                },
            }
        ],
        level="info",
        since_ms=None,
        resume_after=None,
    )

    assert last_activity_at == "2026-05-15T00:00:00Z"
    assert events[0]["event_type"] == "turn_interrupted"
    assert events[0]["progress_kind"] == "turn_interrupted"
    assert events[0]["summary"] == "Turn interrupted"


def test_grouped_progress_tail_event_collects_ids_linearly() -> None:
    grouped_items = [
        ProgressProjectionItem(
            item_id=f"tool:{idx % 700}",
            kind="tool",
            state="completed",
            title="Tool completed",
            summary=f"Tool {idx}",
            event_ids=(idx, idx + 1),
            timestamp="2026-06-25T00:00:00+00:00",
            group_id="group-1",
            group_kind="tool",
            tool_name="shell",
        )
        for idx in range(1, 5001)
    ]

    started = time.perf_counter()
    payload = _tail_event_from_progress_item(
        grouped_items[-1],
        received_at="2026-06-25T00:00:00+00:00",
        progress_items=grouped_items,
    )
    elapsed = time.perf_counter() - started

    assert payload["progress_item_ids"][:3] == ["tool:1", "tool:2", "tool:3"]
    assert len(payload["progress_item_ids"]) == 700
    assert payload["progress_event_ids"][:3] == [1, 2, 3]
    assert payload["progress_event_ids"][-1] == 5001
    assert elapsed < 1.0
