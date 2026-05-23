from __future__ import annotations

from codex_autorunner.core.orchestration.stream_text_merge import (
    AssistantTextAccumulator,
    append_assistant_stream_text_readably,
    merge_assistant_stream_text,
)


def test_merge_assistant_stream_text_returns_incoming_for_empty_current() -> None:
    assert merge_assistant_stream_text("", "hello") == "hello"


def test_merge_assistant_stream_text_keeps_current_for_identical_chunk() -> None:
    assert merge_assistant_stream_text("hello", "hello") == "hello"


def test_merge_assistant_stream_text_replaces_with_prefix_extension() -> None:
    assert merge_assistant_stream_text("hello", "hello world") == "hello world"


def test_merge_assistant_stream_text_appends_only_non_overlapping_suffix() -> None:
    assert merge_assistant_stream_text("hello wor", "world") == "hello world"


def test_merge_assistant_stream_text_concatenates_when_no_overlap() -> None:
    assert merge_assistant_stream_text("alpha", "beta") == "alphabeta"


def test_append_assistant_stream_text_readably_keeps_subword_splits_glued() -> None:
    assert append_assistant_stream_text_readably("inter", "national") == "international"
    assert append_assistant_stream_text_readably("non", "sense") == "nonsense"
    assert append_assistant_stream_text_readably("re", "do") == "redo"


def test_append_assistant_stream_text_readably_preserves_word_boundaries() -> None:
    text = ""
    for chunk in (
        "Confirmed:",
        "**",
        "`/car/hub/read-models/chats`",
        "returns",
        "500",
        "**",
        "everything",
        "else",
        "is",
        "healthy.",
    ):
        text = append_assistant_stream_text_readably(text, chunk)

    assert (
        text
        == "Confirmed: **`/car/hub/read-models/chats` returns 500** everything else is healthy."
    )


def test_assistant_text_accumulator_subword_snapshots_avoid_spurious_spaces() -> None:
    accumulator = AssistantTextAccumulator()
    accumulator.merge_snapshot("inter", preserve_word_boundaries=True)
    accumulator.merge_snapshot("national", preserve_word_boundaries=True)
    assert accumulator.text == "international"


def test_assistant_text_accumulator_records_strict_deltas() -> None:
    accumulator = AssistantTextAccumulator()

    accumulator.append_delta("hello")
    accumulator.append_delta(" world")

    assert accumulator.text == "hello world"


def test_assistant_text_accumulator_records_cumulative_snapshots() -> None:
    accumulator = AssistantTextAccumulator()

    accumulator.merge_snapshot("hello")
    accumulator.merge_snapshot("hello world")

    assert accumulator.text == "hello world"


def test_assistant_text_accumulator_replaces_stream_with_terminal_message() -> None:
    accumulator = AssistantTextAccumulator()

    accumulator.merge_snapshot("draft answer")
    accumulator.replace_final("final canonical answer")

    assert accumulator.text == "final canonical answer"
    assert accumulator.stream_text == "draft answer"


def test_assistant_text_accumulator_empty_and_duplicate_chunks_are_stable() -> None:
    accumulator = AssistantTextAccumulator()

    accumulator.merge_snapshot("")
    accumulator.merge_snapshot("hello")
    accumulator.merge_snapshot("hello")
    accumulator.append_delta("")

    assert accumulator.text == "hello"
