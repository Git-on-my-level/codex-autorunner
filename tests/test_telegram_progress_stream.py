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
    assert "output: Investigating run event ordering" in rendered
    assert "late item/completed" in rendered
    assert len([a for a in tracker.actions if a.label == "output"]) == 1


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
    assert "output: Final response text" in rendered_after_output


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
    assert "output:" in rendered
    assert "second output" in rendered


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

    assert "output:" in rendered
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
