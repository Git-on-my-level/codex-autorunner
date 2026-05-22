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


def test_reduce_turn_output_trims_after_latest_prior_segment() -> None:
    state = RuntimeThreadRunEventState()

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-3",
        backend_thread_id="session-1",
        backend_turn_id="turn-3",
        outcome=_outcome("first answer\n\nsecond answer\n\nthird answer"),
        event_state=state,
        prior_assistant_texts=["second answer"],
    )

    assert envelope.text == "third answer"
    assert envelope.ownership == "trimmed_from_cumulative"
    assert envelope.source == "reducer"


def test_reduce_turn_output_uses_last_assistant_section_from_transcript_shaped_output() -> (
    None
):
    state = RuntimeThreadRunEventState()

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome(
            "User:\nfirst question\n\n"
            "Assistant:\nfirst answer\n\n"
            "User:\nsecond question\n\n"
            "Assistant:\nsecond answer"
        ),
        event_state=state,
        prior_assistant_texts=["first answer"],
    )

    assert envelope.text == "second answer"
    assert envelope.ownership == "current_turn"
    assert envelope.source == "reducer"
    assert envelope.provenance["candidate_source"] == "outcome"


def test_reduce_turn_output_trims_mutated_cumulative_transcript_prefix() -> None:
    state = RuntimeThreadRunEventState()
    previous = "\n".join(
        [
            "Earlier diagnosis.",
            "",
            "```text",
            "/Users/example/.local/pipx/venvs/codex-autorunner.next-20260518-024620/bin/python",
            "```",
            "",
            *[
                f"Step {index}: verify the installed package and restart state."
                for index in range(80)
            ],
            "The package needs to be installed in that venv.",
        ]
    )
    current = (
        previous.replace("```text\n", "``\n", 1).replace(
            "Earlier diagnosis.\n\n```text",
            "Earlier diagnosis.\n```text",
            1,
        )
        + "\n\nCurrent turn answer only."
    )

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome(current),
        event_state=state,
        prior_assistant_texts=[previous],
    )

    assert envelope.text == "Current turn answer only."
    assert envelope.ownership == "trimmed_from_cumulative"
    assert envelope.source == "reducer"


def test_reduce_turn_output_keeps_new_answer_when_near_prefix_tail_repeats() -> None:
    state = RuntimeThreadRunEventState()
    previous = "prefix block with ABCD near tail\n" + ("filler line\n" * 60) + "ABCD"
    current = previous[:-4] + "WXYZ" + "ABCD extra answer"

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome(current),
        event_state=state,
        prior_assistant_texts=[previous],
    )

    assert envelope.text == "ABCD extra answer"
    assert envelope.ownership == "trimmed_from_cumulative"
    assert envelope.source == "reducer"


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


def test_turn_output_evidence_does_not_include_matched_prior_text() -> None:
    state = RuntimeThreadRunEventState()
    prior_text = "first answer"

    envelope = reduce_turn_output(
        managed_thread_id="thread-1",
        managed_turn_id="turn-2",
        backend_thread_id="session-1",
        backend_turn_id="turn-2",
        outcome=_outcome(prior_text),
        event_state=state,
        prior_assistant_texts=[prior_text],
    )

    assert envelope.matched_prior_text == prior_text
    assert envelope.evidence["turn_output_prior_chars"] == len(prior_text)
    assert envelope.evidence["turn_output_provenance"]["candidate_source"] == (
        "prior_guard"
    )
    assert "matched_prior_text" not in envelope.evidence["turn_output_provenance"]
    assert prior_text not in str(envelope.evidence)


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
