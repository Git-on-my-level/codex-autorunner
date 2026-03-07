from codex_autorunner.integrations.telegram.progress_stream import (
    TurnProgressTracker,
    render_progress_text,
)


def test_render_progress_text_subagent_thinking_block() -> None:
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
    assert "🤖 @subagent thinking" in rendered
    assert "Subagent planning" in rendered


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


def test_note_thinking_keeps_distinct_snapshots() -> None:
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
    assert len(thinking_actions) == 2
    rendered = render_progress_text(tracker, max_length=2000, now=2.0)
    assert "Checking out-of-order completion handling" in rendered
    assert "Designing settle window for finalization" in rendered
