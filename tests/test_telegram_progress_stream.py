from codex_autorunner.integrations.telegram.progress_stream import (
    TurnProgressTracker,
    render_progress_text,
)


def test_render_progress_text_shows_only_latest_transient_thinking() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="opencode",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.add_action(
        "thinking",
        "Subagent planning",
        "update",
        item_id="subagent:1",
        subagent_label="@subagent",
    )
    tracker.note_thinking("Parent thinking")
    rendered = render_progress_text(tracker, max_length=2000, now=0.0)
    assert "🧠 Parent thinking" in rendered
    assert "🤖 @subagent thinking" not in rendered
    assert "Subagent planning" not in rendered


def test_note_output_accumulates_across_updates() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.note_output("Investigating run event ordering")
    tracker.note_output("found turn/completed before late item/completed")

    rendered = render_progress_text(tracker, max_length=2000, now=1.0)
    assert "Investigating run event ordering" in rendered
    assert "late item/completed" in rendered
    assert len([a for a in tracker.actions if a.label == "output"]) == 1


def test_note_output_does_not_insert_artificial_spaces_between_chunks() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.note_output("orches")
    tracker.note_output("tration")
    tracker.note_output(" isn't")
    tracker.note_output(" broken")

    rendered = render_progress_text(tracker, max_length=2000, now=1.0)

    assert "orchestration isn't broken" in rendered
    assert "orches tration" not in rendered


def test_note_output_does_not_drop_internal_substring_chunks() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.note_output("abcdef")
    tracker.note_output("cd")

    rendered = render_progress_text(tracker, max_length=2000, now=1.0)

    assert "abcdefcd" in rendered


def test_note_thinking_is_transient_and_cleared_by_output() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.note_thinking("Checking out-of-order completion handling")
    tracker.note_thinking("Designing settle window for finalization")

    thinking_actions = [a for a in tracker.actions if a.label == "thinking"]
    assert len(thinking_actions) == 0
    rendered = render_progress_text(tracker, max_length=2000, now=2.0)
    assert "Designing settle window for finalization" in rendered
    tracker.note_output("Final response text")
    rendered_after_output = render_progress_text(tracker, max_length=2000, now=3.0)
    assert "Designing settle window for finalization" not in rendered_after_output
    assert "Final response text" in rendered_after_output


def test_note_tool_streams_without_persisting_history() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.note_tool("read_file")
    tracker.note_tool("run_tests")

    tool_actions = [a for a in tracker.actions if a.label == "tool"]
    assert len(tool_actions) == 0
    rendered = render_progress_text(tracker, max_length=2000, now=2.0)
    assert "tool: run_tests" in rendered
    assert "tool: read_file" not in rendered


def test_output_after_transient_events_remains_visible() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=2,
        max_output_chars=200,
    )
    tracker.note_output("first output")
    tracker.add_action("item", "non-output event one", "done")
    tracker.add_action("item", "non-output event two", "done")
    tracker.note_tool("run_tests")
    tracker.note_output("second output")

    rendered = render_progress_text(tracker, max_length=2000, now=3.0)
    assert "second output" in rendered


def test_live_mode_keeps_latest_output_visible_when_window_is_full_of_non_output() -> (
    None
):
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=3,
        max_output_chars=200,
    )
    tracker.note_output("important output")
    tracker.add_action("notice", "notice one", "update")
    tracker.add_action("notice", "notice two", "update")
    tracker.add_action("notice", "notice three", "update")

    rendered = render_progress_text(tracker, max_length=2000, now=3.0)

    assert "important output" in rendered


def test_render_progress_text_keeps_output_block_when_message_budget_is_tight() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=10_000,
    )
    tracker.note_output("prefix " + ("x" * 600) + " tail")

    rendered = render_progress_text(tracker, max_length=160, now=1.0)

    assert "tail" in rendered


def test_render_progress_text_prefers_subagent_thinking_content_in_fallback() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=10_000,
    )
    tracker.add_action(
        "thinking",
        "subagent summary " + ("x" * 600),
        "update",
        item_id="subagent:1",
        subagent_label="@subagent",
    )

    rendered = render_progress_text(tracker, max_length=90, now=1.0)

    assert "subagent summary" in rendered
    assert not rendered.endswith("\n---")


def test_live_fallback_reserves_trace_lines_and_keeps_output_tail() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=10_000,
    )
    tracker.note_tool("run_tests --all")
    tracker.note_thinking("investigating truncation behavior")
    tracker.note_output("prefix " + ("x" * 800) + " tail")

    rendered = render_progress_text(tracker, max_length=200, now=1.0)

    assert "tail" in rendered
    assert "🧠 investigating truncation behavior" in rendered
    assert "tool: run_tests --all" in rendered
    assert len(rendered) <= 200


def test_live_fallback_can_use_persisted_traces_after_transient_clears() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=10_000,
    )
    tracker.note_tool("run_tests")
    tracker.note_thinking("checking parser edge cases")
    # This clears transient_action; fallback should still use persisted traces.
    tracker.note_output("start " + ("y" * 900) + " done")

    rendered = render_progress_text(tracker, max_length=180, now=1.0)

    assert "done" in rendered
    assert "checking parser edge cases" in rendered
    assert "tool: run_tests" in rendered


def test_render_progress_text_final_mode_keeps_only_output_when_available() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=10_000,
    )
    tracker.note_output("intermediate output")
    tracker.add_action("files", "updated a file", "done")
    tracker.note_tool("run_tests")
    tracker.note_thinking("considering alternatives")

    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert rendered == "intermediate output"
    assert "files:" not in rendered
    assert "run_tests" not in rendered
    assert "considering alternatives" not in rendered


def test_note_output_preserves_internal_whitespace() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=200,
    )
    tracker.note_output("line one\n  line two")
    tracker.note_output("value  with  double spaces")

    rendered = render_progress_text(tracker, max_length=2000, now=1.0)

    assert "line one\n  line two" in rendered
    assert "value  with  double spaces" in rendered


def test_final_mode_keeps_output_even_when_compact_window_excludes_it() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=1,
        max_output_chars=200,
    )
    tracker.note_output("earlier output")
    tracker.add_action("files", "later non-output action", "done")

    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert rendered == "earlier output"
    assert "files:" not in rendered


def test_render_progress_text_keeps_commentary_live_only() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=500,
    )
    tracker.note_output("streamed output")
    tracker.note_commentary("interim commentary block")

    live = render_progress_text(tracker, max_length=2000, now=1.0)
    final = render_progress_text(tracker, max_length=2000, now=1.0, render_mode="final")

    assert "streamed output" in live
    assert "interim commentary block" in live
    assert "Interim note from agent while this turn is still running:" not in live
    assert "Final reply will be sent separately when the turn completes." not in live
    assert "streamed output" in final
    assert "interim commentary block" not in final


def test_final_mode_uses_consolidated_output_after_interleaved_actions() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=500,
    )
    tracker.note_output("first output")
    tracker.note_tool("run_tests")
    tracker.note_output("second output")

    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert rendered.count("first output") == 1
    assert rendered.count("second output") == 1
    assert "run_tests" not in rendered


def test_final_mode_accumulates_segmented_outputs() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=500,
    )
    tracker.note_output("intermediate output one")
    tracker.note_output("intermediate output two", new_segment=True)

    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert "intermediate output one" in rendered
    assert "intermediate output two" in rendered
    assert "\n\n" in rendered


def test_end_output_segment_forces_next_output_into_new_block() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=500,
    )
    tracker.note_output("output one")
    tracker.end_output_segment()
    tracker.note_output("output two")

    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert "output one" in rendered
    assert "output two" in rendered
    assert "\n\n" in rendered


def test_drop_terminal_output_if_duplicate_removes_matching_final_block() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=500,
    )
    tracker.note_output("intermediate output")
    tracker.end_output_segment()
    tracker.note_output("final answer")

    dropped = tracker.drop_terminal_output_if_duplicate("final answer")
    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert dropped is True
    assert "intermediate output" in rendered
    assert "final answer" not in rendered


def test_drop_terminal_output_if_duplicate_matches_truncated_tail() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=20,
    )
    final_text = "this is a very long final answer that gets truncated"
    tracker.note_output("intermediate output")
    tracker.end_output_segment()
    tracker.note_output(final_text)

    dropped = tracker.drop_terminal_output_if_duplicate(final_text)
    rendered = render_progress_text(
        tracker, max_length=2000, now=1.0, render_mode="final"
    )

    assert dropped is True
    assert "intermediate output" in rendered
    assert "final answer" not in rendered


def test_drop_terminal_output_if_duplicate_scans_past_newer_nonmatching_outputs() -> (
    None
):
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=500,
    )
    tracker.note_output("intermediate output")
    tracker.end_output_segment()
    tracker.note_output("terminal final answer")
    tracker.end_output_segment()
    tracker.note_output("post-final notice")

    dropped = tracker.drop_terminal_output_if_duplicate("terminal final answer")
    output_blocks = [
        action.text for action in tracker.actions if action.label == "output"
    ]

    assert dropped is True
    assert output_blocks == ["intermediate output", "post-final notice"]


def test_final_mode_output_truncates_from_tail() -> None:
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="mock-model",
        label="working",
        max_actions=10,
        max_output_chars=10_000,
    )
    tracker.note_output("prefix " + ("x" * 500) + " tail")

    rendered = render_progress_text(
        tracker, max_length=80, now=1.0, render_mode="final"
    )

    assert "tail" in rendered
    assert rendered.startswith("...")
