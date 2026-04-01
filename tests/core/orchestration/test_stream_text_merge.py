from __future__ import annotations

from codex_autorunner.core.orchestration.stream_text_merge import (
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
