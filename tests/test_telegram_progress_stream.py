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
    assert "--- @subagent thinking ---" in rendered
    assert "Subagent planning" in rendered
