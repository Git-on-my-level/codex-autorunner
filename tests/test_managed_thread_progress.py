from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    Completed,
    OutputDelta,
    RunNotice,
    ToolCall,
)
from codex_autorunner.integrations.chat.managed_thread_progress import (
    ProgressRuntimeState,
    apply_run_event_to_progress_tracker,
)
from codex_autorunner.integrations.chat.progress_primitives import (
    TurnProgressTracker,
    render_progress_text,
)


def _tracker() -> TurnProgressTracker:
    return TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="default",
        label="working",
        max_actions=12,
        max_output_chars=400,
    )


def test_apply_run_event_to_progress_tracker_records_tool_calls() -> None:
    tracker = _tracker()

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        ToolCall(timestamp="2026-03-15T00:00:00Z", tool_name="exec", tool_input={}),
        runtime_state=ProgressRuntimeState(),
    )

    assert outcome.changed is True
    assert outcome.force is True
    assert outcome.terminal is False
    assert tracker.transient_action is not None
    assert tracker.transient_action.label == "tool"
    assert tracker.transient_action.text == "exec"


def test_apply_run_event_to_progress_tracker_forces_first_thinking_notice() -> None:
    tracker = _tracker()

    first = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:00Z",
            kind="thinking",
            message="checking Discord progress updates",
        ),
        runtime_state=ProgressRuntimeState(),
    )
    second = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:01Z",
            kind="thinking",
            message="checking Discord progress updates in more detail",
        ),
        runtime_state=ProgressRuntimeState(),
    )

    assert first.changed is True
    assert first.force is True
    assert second.changed is True
    assert second.force is False


def test_apply_run_event_to_progress_tracker_updates_log_line_output() -> None:
    tracker = _tracker()

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="2026-03-15T00:00:00Z",
            content="Tokens used: 10",
            delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
        ),
        runtime_state=ProgressRuntimeState(),
    )

    assert outcome.changed is True
    assert tracker.actions[-1].label == "output"
    assert tracker.actions[-1].text == "Tokens used: 10"
    assert tracker.actions[-1].item_id == "opencode:token-usage"


def test_apply_run_event_to_progress_tracker_finalizes_completed_turn() -> None:
    tracker = _tracker()
    runtime_state = ProgressRuntimeState()
    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="2026-03-15T00:00:00Z",
            content="intermediate text",
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        ),
        runtime_state=runtime_state,
    )

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        Completed(
            timestamp="2026-03-15T00:00:01Z",
            final_message="final answer",
        ),
        runtime_state=runtime_state,
    )

    assert outcome.changed is True
    assert outcome.terminal is True
    assert outcome.render_mode == "final"
    assert tracker.label == "done"
    assert runtime_state.final_message == "final answer"


def test_apply_run_event_to_progress_tracker_marks_interrupts_terminal() -> None:
    tracker = _tracker()

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:00Z",
            kind="interrupted",
            message="Turn interrupted",
        ),
        runtime_state=ProgressRuntimeState(),
    )

    assert outcome.changed is True
    assert outcome.force is True
    assert outcome.terminal is True
    assert tracker.label == "cancelled"


def test_apply_run_event_to_progress_tracker_replaces_cumulative_snapshot_in_place() -> (
    None
):
    tracker = _tracker()
    runtime_state = ProgressRuntimeState()

    first = "I’m re-scoping to March 20, 2026 only"
    second = "I’m re-scoping to March 20, 2026 only and tracing the exact log lines."

    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="2026-03-15T00:00:00Z",
            content=first,
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        ),
        runtime_state=runtime_state,
    )
    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="2026-03-15T00:00:01Z",
            content=second,
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        ),
        runtime_state=runtime_state,
    )

    output_actions = [action for action in tracker.actions if action.label == "output"]
    assert len(output_actions) == 1
    assert output_actions[0].text == second

    rendered = render_progress_text(tracker, max_length=2000, now=1.0)
    assert rendered.count("I’m re-scoping to March 20, 2026 only") == 1


def test_apply_run_event_to_progress_tracker_preserves_boundary_between_snapshots() -> (
    None
):
    tracker = _tracker()
    runtime_state = ProgressRuntimeState()

    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="2026-03-15T00:00:00Z",
            content="Scanning repo",
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        ),
        runtime_state=runtime_state,
    )
    tracker.end_output_segment()
    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="2026-03-15T00:00:01Z",
            content="Scanning repo complete.",
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        ),
        runtime_state=runtime_state,
    )

    output_actions = [action for action in tracker.actions if action.label == "output"]
    assert [action.text for action in output_actions] == [
        "Scanning repo",
        "Scanning repo complete.",
    ]
