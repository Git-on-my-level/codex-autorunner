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


def test_append_assistant_stream_text_readably_keeps_markdown_and_path_tokens_glued() -> (
    None
):
    text = ""
    for chunk in (
        "**Current",
        " State",
        "**\n\n",
        "**AG",
        "ENTS.md",
        "**: ",
        "Current",
        "\n- Workspace",
        " path",
        ": ",
        "`/Users/d",
        "az",
        "heng/car-work",
        "space/cod",
        "ex-aut",
        "orunner`",
        "\n\n|",
        " Section",
        " |",
        " Count",
        " |\n|",
        "---",
        "|",
        "---",
        ":",
        "|\n| PMA",
        " file",
        " inbox",
        " |",
        " 5",
        " |",
    ):
        text = append_assistant_stream_text_readably(text, chunk)

    assert text == (
        "**Current State**\n\n"
        "**AGENTS.md**: Current\n"
        "- Workspace path: `/Users/dazheng/car-workspace/codex-autorunner`\n\n"
        "| Section | Count |\n"
        "|---|---:|\n"
        "| PMA file inbox | 5 |"
    )


def test_append_assistant_stream_text_readably_keeps_opening_bold_delimiter_tight() -> (
    None
):
    text = ""
    for chunk in (
        "**",
        "Recommendation",
        ":",
        " No cleanup needed right now.",
        "**",
    ):
        text = append_assistant_stream_text_readably(text, chunk)

    assert text == "**Recommendation: No cleanup needed right now.**"


def test_append_assistant_stream_text_readably_keeps_word_boundary_before_new_bold_span() -> (
    None
):
    assert (
        append_assistant_stream_text_readably("See", "**AGENTS.md**")
        == "See **AGENTS.md**"
    )


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


def test_assistant_text_accumulator_mixed_spacing_snapshots_keep_fallback() -> None:
    accumulator = AssistantTextAccumulator()

    for chunk in (
        "Confirmed:",
        "\n",
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
        accumulator.merge_snapshot(chunk, preserve_word_boundaries=True)

    assert (
        accumulator.text
        == "Confirmed:\n**`/car/hub/read-models/chats` returns 500** everything else is healthy."
    )


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
