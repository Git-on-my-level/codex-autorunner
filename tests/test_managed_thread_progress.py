from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    ApprovalRequested,
    Completed,
    OutputDelta,
    RunNotice,
    ToolCall,
)
from codex_autorunner.integrations.chat.managed_thread_progress import (
    ProgressRuntimeState,
    apply_run_event_to_progress_tracker,
)
from codex_autorunner.integrations.chat.managed_thread_progress_projector import (
    ManagedThreadProgressProjector,
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


def _projector() -> ManagedThreadProgressProjector:
    return ManagedThreadProgressProjector(
        _tracker(),
        min_render_interval_seconds=1.0,
        heartbeat_interval_seconds=5.0,
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


def test_progress_notice_shows_when_no_transient_action() -> None:
    tracker = _tracker()

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:00Z",
            kind="progress",
            message="agent busy",
        ),
        runtime_state=ProgressRuntimeState(),
    )

    assert outcome.changed is True
    assert tracker.transient_action is not None
    assert tracker.transient_action.label == "notice"
    assert tracker.transient_action.text == "agent busy"
    assert len(tracker.actions) == 0


def test_progress_notice_does_not_overwrite_tool_trace() -> None:
    tracker = _tracker()

    apply_run_event_to_progress_tracker(
        tracker,
        ToolCall(timestamp="2026-03-15T00:00:00Z", tool_name="exec", tool_input={}),
        runtime_state=ProgressRuntimeState(),
    )
    assert tracker.transient_action is not None
    assert tracker.transient_action.label == "tool"

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:01Z",
            kind="progress",
            message="agent busy",
        ),
        runtime_state=ProgressRuntimeState(),
    )

    assert outcome.changed is False
    assert tracker.transient_action.label == "tool"
    assert tracker.transient_action.text == "exec"
    assert len(tracker.actions) == 0


def test_progress_notice_does_not_overwrite_thinking_trace() -> None:
    tracker = _tracker()

    apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:00Z",
            kind="thinking",
            message="analyzing code",
        ),
        runtime_state=ProgressRuntimeState(),
    )
    assert tracker.transient_action is not None
    assert tracker.transient_action.label == "thinking"

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:01Z",
            kind="progress",
            message="agent busy",
        ),
        runtime_state=ProgressRuntimeState(),
    )

    assert outcome.changed is False
    assert tracker.transient_action.label == "thinking"
    assert tracker.transient_action.text == "analyzing code"


def test_progress_notice_does_not_accumulate_in_action_list() -> None:
    tracker = _tracker()

    for i in range(5):
        apply_run_event_to_progress_tracker(
            tracker,
            RunNotice(
                timestamp=f"2026-03-15T00:00:0{i}Z",
                kind="progress",
                message="agent busy",
            ),
            runtime_state=ProgressRuntimeState(),
        )

    assert len(tracker.actions) == 0
    assert tracker.transient_action is not None
    assert tracker.transient_action.text == "agent busy"


def test_tool_call_replaces_progress_notice() -> None:
    tracker = _tracker()

    apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(
            timestamp="2026-03-15T00:00:00Z",
            kind="progress",
            message="agent busy",
        ),
        runtime_state=ProgressRuntimeState(),
    )
    assert tracker.transient_action is not None
    assert tracker.transient_action.label == "notice"

    apply_run_event_to_progress_tracker(
        tracker,
        ToolCall(timestamp="2026-03-15T00:00:01Z", tool_name="read", tool_input={}),
        runtime_state=ProgressRuntimeState(),
    )

    assert tracker.transient_action is not None
    assert tracker.transient_action.label == "tool"
    assert tracker.transient_action.text == "read"


def test_interleaved_busy_and_tool_events_preserves_tools() -> None:
    """Simulates real OpenCode flow: busy → tool → busy → thinking → busy."""
    tracker = _tracker()
    state = ProgressRuntimeState()

    apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(timestamp="t0", kind="progress", message="agent busy"),
        runtime_state=state,
    )
    assert tracker.transient_action.label == "notice"

    apply_run_event_to_progress_tracker(
        tracker,
        ToolCall(timestamp="t1", tool_name="exec", tool_input={}),
        runtime_state=state,
    )
    assert tracker.transient_action.label == "tool"

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(timestamp="t2", kind="progress", message="agent busy"),
        runtime_state=state,
    )
    assert outcome.changed is False
    assert tracker.transient_action.label == "tool"

    apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(timestamp="t3", kind="thinking", message="planning next step"),
        runtime_state=state,
    )
    assert tracker.transient_action.label == "thinking"

    outcome = apply_run_event_to_progress_tracker(
        tracker,
        RunNotice(timestamp="t4", kind="progress", message="agent busy"),
        runtime_state=state,
    )
    assert outcome.changed is False
    assert tracker.transient_action.label == "thinking"
    assert tracker.transient_action.text == "planning next step"

    assert len(tracker.actions) == 0


def test_assistant_message_does_not_duplicate_when_stream_output_is_truncated() -> None:
    """Regression: when streamed output exceeds max_output_chars, the stored
    text is truncated.  A subsequent ASSISTANT_MESSAGE with the full text
    would fail the startsWith check and create a duplicate output action.
    """
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="default",
        label="working",
        max_output_chars=80,
    )
    state = ProgressRuntimeState()

    long_stream = (
        "I'm investigating the server startup delay by tracing the request "
        "lifecycle from the web entrypoint through each middleware layer."
    )
    assert len(long_stream) > 80

    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="t0",
            content=long_stream,
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        ),
        runtime_state=state,
    )

    full_message = long_stream + " The root cause is a blocking sleep call."
    apply_run_event_to_progress_tracker(
        tracker,
        OutputDelta(
            timestamp="t1",
            content=full_message,
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
        ),
        runtime_state=state,
    )

    output_actions = [a for a in tracker.actions if a.label == "output"]
    assert len(output_actions) == 1, (
        f"Expected single output action but got {len(output_actions)}; "
        f"texts={[a.text for a in output_actions]}"
    )

    rendered = render_progress_text(tracker, max_length=4000, now=1.0)
    assert rendered.count("investigating") <= 1


def test_managed_thread_progress_projector_records_semantic_phase_sequence() -> None:
    projector = _projector()
    projector.mark_queued()
    projector.mark_working()

    events = [
        ApprovalRequested(
            timestamp="2026-03-15T00:00:00Z",
            request_id="req-1",
            description="Need approval to run tests",
            context={},
        ),
        ToolCall(
            timestamp="2026-03-15T00:00:01Z",
            tool_name="exec",
            tool_input={"cmd": "pytest -q"},
        ),
        Completed(
            timestamp="2026-03-15T00:00:02Z",
            final_message="tests passed",
        ),
    ]

    for event in events:
        projector.apply_run_event(event)

    assert projector.phase_sequence() == (
        "queued",
        "working",
        "approval",
        "progress",
        "terminal",
    )
    assert projector.semantic_phase == "terminal"
    assert projector.tracker.label == "done"


def test_managed_thread_progress_projector_reuses_then_supersedes_anchor() -> None:
    projector = _projector()
    projector.mark_queued()

    first = projector.bind_anchor("msg-1", owned=True, reused=False)
    reused = projector.bind_anchor("msg-1", owned=True, reused=True)
    projector.mark_working()
    superseded = projector.bind_anchor("msg-2", owned=True, reused=False)

    assert first.anchor_ref == "msg-1"
    assert first.stage == "queued"
    assert reused.anchor_ref == "msg-1"
    assert reused.reused is True
    assert superseded.anchor_ref == "msg-2"
    assert superseded.superseded_anchor_ref == "msg-1"
    assert superseded.cleanup_required is True


def test_managed_thread_progress_projector_controls_duplicate_and_heartbeat_policy() -> (
    None
):
    projector = _projector()
    projector.mark_working()
    rendered = "working · agent codex · default · 0s"

    assert projector.should_emit_render(rendered, now=1.0, force=False) is True
    projector.note_rendered(rendered, now=1.0)

    assert projector.should_emit_render(rendered, now=1.5, force=False) is False
    assert projector.should_emit_render(rendered, now=2.5, force=False) is False
    assert projector.should_emit_heartbeat(now=5.9) is False
    assert projector.should_emit_heartbeat(now=6.0) is True
