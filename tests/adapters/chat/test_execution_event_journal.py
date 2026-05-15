from __future__ import annotations

from codex_autorunner.adapters.chat.execution_event_journal import (
    journal_events_from_timeline_entries,
)


def test_journal_replays_turn_interrupted_as_interrupted_event() -> None:
    journal = journal_events_from_timeline_entries(
        [
            {
                "event_index": 3,
                "event_type": "turn_interrupted",
                "event": {
                    "timestamp": "2026-05-15T00:00:00Z",
                    "reason": "Runtime thread interrupted",
                },
            }
        ],
        include_derived_events=False,
    )

    assert len(journal) == 1
    assert journal[0].domain == "execution"
    assert journal[0].name == "interrupted"
    assert journal[0].status == "interrupted"
    assert journal[0].source_event_type == "interrupted"
    assert journal[0].message == "Runtime thread interrupted"
