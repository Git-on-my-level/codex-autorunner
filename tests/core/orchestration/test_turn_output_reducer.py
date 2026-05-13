from __future__ import annotations

from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadOutcome
from codex_autorunner.core.orchestration.turn_output_reducer import reduce_turn_output


def _outcome(text: str) -> RuntimeThreadOutcome:
    return RuntimeThreadOutcome(
        status="ok",
        assistant_text=text,
        error=None,
        backend_thread_id="session-1",
        backend_turn_id="turn-1",
    )


def test_reduce_turn_output_trims_cumulative_transcript_prefix() -> None:
    state = RuntimeThreadRunEventState()

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome("first answer\n\nsecond answer"),
        event_state=state,
        prior_assistant_texts=["first answer"],
    )

    assert envelope.text == "second answer"
    assert envelope.ownership == "trimmed_from_cumulative"
    assert envelope.source == "reducer"
    assert envelope.provenance["candidate_source"] == "outcome"


def test_reduce_turn_output_rejects_exact_prior_output_as_stale() -> None:
    state = RuntimeThreadRunEventState()

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome("first answer"),
        event_state=state,
        prior_assistant_texts=["first answer"],
    )

    assert envelope.text == ""
    assert envelope.ownership == "rejected_stale_prior"
    assert envelope.source == "reducer"
    assert envelope.provenance["candidate_source"] == "prior_guard"


def test_reduce_turn_output_uses_stream_text_when_terminal_text_is_stale() -> None:
    state = RuntimeThreadRunEventState()
    state.note_stream_text("second answer")

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome("first answer"),
        event_state=state,
        prior_assistant_texts=["first answer"],
    )

    assert envelope.text == "second answer"
    assert envelope.ownership == "current_turn_stream"
    assert envelope.source == "runtime_stream"
    assert envelope.provenance["candidate_source"] == "event_stream"
