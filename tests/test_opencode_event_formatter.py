from codex_autorunner.agents.opencode.logging import OpenCodeEventFormatter


def test_opencode_event_formatter_coalesces_reasoning_deltas() -> None:
    fmt = OpenCodeEventFormatter()
    part = {"id": "r1", "type": "reasoning"}

    # OpenCode can stream reasoning at token-level granularity. We should not emit
    # a log line per delta chunk.
    assert fmt.format_part("reasoning", part, "Hello ") == ["thinking"]
    assert fmt.format_part("reasoning", part, "world") == []

    # Flush once at end-of-turn.
    assert fmt.flush_all_reasoning() == ["**Hello world**"]
    assert fmt.flush_all_reasoning() == []


def test_opencode_event_formatter_emits_complete_lines_on_newlines() -> None:
    fmt = OpenCodeEventFormatter()
    part = {"id": "r1", "type": "reasoning"}

    # If the stream includes a newline, emit the completed line immediately,
    # leaving the remainder buffered for a final flush.
    assert fmt.format_part("reasoning", part, "Hello\nworld") == [
        "thinking",
        "**Hello**",
    ]
    assert fmt.flush_all_reasoning() == ["**world**"]
