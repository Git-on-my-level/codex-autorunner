from __future__ import annotations

from codex_autorunner.core.pma.tail_serialization import build_live_activity_projection


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
