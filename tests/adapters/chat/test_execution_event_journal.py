from __future__ import annotations

from codex_autorunner.adapters.chat.execution_event_journal import (
    journal_events_from_run_events,
    journal_events_from_timeline_entries,
)
from codex_autorunner.core.ports.run_event import UserInputRequested


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


def test_journal_records_user_input_requests() -> None:
    journal = journal_events_from_run_events(
        [
            UserInputRequested(
                timestamp="2026-05-15T00:00:00Z",
                request_id="question-1",
                description="Which framework?",
                questions=({"id": "framework", "text": "Which framework?"},),
                context={"source": "opencode"},
            )
        ]
    )

    assert len(journal) == 1
    assert journal[0].domain == "user_input"
    assert journal[0].name == "requested"
    assert journal[0].status == "requested"
    assert journal[0].source_event_type == "user_input_requested"
    assert journal[0].data["request_id"] == "question-1"
