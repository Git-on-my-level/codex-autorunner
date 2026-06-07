from __future__ import annotations

from codex_autorunner.core.orchestration.progress_projection import (
    ProgressProjectionInput,
    ProgressProjectionState,
    project_progress_events,
    reduce_progress_event,
)
from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    RUN_EVENT_STREAM_MODE_SNAPSHOT,
    ApprovalRequested,
    Failed,
    Interrupted,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
    UserInputRequested,
)


def _project(*events):
    return project_progress_events(
        [
            ProgressProjectionInput(
                event_id=index,
                timestamp=f"2026-05-06T10:00:0{index}Z",
                event=event,
            )
            for index, event in enumerate(events, start=1)
        ]
    )


def test_progress_projection_merges_contiguous_assistant_updates() -> None:
    items = _project(
        RunNotice(timestamp="2026-05-06T10:00:01Z", kind="thinking", message="Reading"),
        OutputDelta(timestamp="2026-05-06T10:00:02Z", content=" files"),
    )

    assert len(items) == 1
    assert items[0].kind == "assistant_update"
    assert items[0].summary == "Reading files"
    assert items[0].event_ids == (1, 2)


def test_progress_projection_replaces_cumulative_assistant_snapshots() -> None:
    items = _project(
        OutputDelta(
            timestamp="2026-05-06T10:00:01Z",
            content="Reading",
            stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
        ),
        OutputDelta(
            timestamp="2026-05-06T10:00:02Z",
            content="Reading files",
            stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
        ),
        OutputDelta(
            timestamp="2026-05-06T10:00:03Z",
            content="Reading files now",
            stream_mode=RUN_EVENT_STREAM_MODE_SNAPSHOT,
        ),
    )

    assert len(items) == 1
    assert items[0].kind == "assistant_update"
    assert items[0].summary == "Reading files now"
    assert items[0].event_ids == (1, 2, 3)
    assert items[0].merge_strategy == RUN_EVENT_STREAM_MODE_SNAPSHOT


def test_progress_projection_keeps_strict_assistant_deltas_append_only() -> None:
    items = _project(
        OutputDelta(timestamp="2026-05-06T10:00:01Z", content="Reading"),
        OutputDelta(timestamp="2026-05-06T10:00:02Z", content=" files"),
        OutputDelta(timestamp="2026-05-06T10:00:03Z", content=" now"),
    )

    assert len(items) == 1
    assert items[0].summary == "Reading files now"
    assert items[0].merge_strategy == "delta"


def test_progress_projection_marks_internal_run_notices_hidden() -> None:
    state = ProgressProjectionState()
    items = [
        reduce_progress_event(
            state,
            ProgressProjectionInput(
                event_id=index,
                timestamp=f"2026-05-06T10:00:0{index}Z",
                event=RunNotice(
                    timestamp=f"2026-05-06T10:00:0{index}Z",
                    kind=kind,
                    message="internal telemetry",
                ),
            ),
        )
        for index, kind in enumerate(
            ("chat_execution_journal", "compaction_summary", "decode_failure"),
            start=1,
        )
    ]

    assert all(item is not None and item.kind == "hidden" for item in items)
    assert all(item is not None and item.hidden for item in items)
    assert (
        _project(
            RunNotice(
                timestamp="2026-05-06T10:00:01Z",
                kind="chat_execution_journal",
                message="terminal=3977ms",
            )
        )
        == []
    )


def test_progress_projection_groups_tool_call_and_result_pairs() -> None:
    items = _project(
        ToolCall(
            timestamp="2026-05-06T10:00:01Z",
            tool_name="pytest",
            tool_input={"path": "tests"},
        ),
        ToolResult(
            timestamp="2026-05-06T10:00:02Z",
            tool_name="pytest",
            status="ok",
            result="passed",
        ),
    )

    assert [item.kind for item in items] == ["tool", "tool"]
    assert [item.state for item in items] == ["started", "completed"]
    assert items[0].event_ids == (1,)
    assert items[1].event_ids == (1, 2)
    assert items[0].group_id == items[1].group_id
    assert items[0].group_kind == "tool_group"


def test_progress_projection_marks_tool_failures() -> None:
    items = _project(
        ToolCall(timestamp="2026-05-06T10:00:01Z", tool_name="pnpm", tool_input={}),
        ToolResult(
            timestamp="2026-05-06T10:00:02Z",
            tool_name="pnpm",
            status="error",
            error="exit 1",
        ),
    )

    assert items[-1].kind == "tool"
    assert items[-1].state == "failed"
    assert items[-1].event_ids == (1, 2)


def test_progress_projection_marks_turn_failure_and_interruption() -> None:
    failed, interrupted, stopped, direct_interrupted = _project(
        Failed(timestamp="2026-05-06T10:00:01Z", error_message="boom"),
        Failed(timestamp="2026-05-06T10:00:02Z", error_message="cancelled by user"),
        Failed(
            timestamp="2026-05-06T10:00:03Z",
            error_message="Stopped by user request.",
        ),
        Interrupted(
            timestamp="2026-05-06T10:00:04Z",
            reason="surface-specific interrupt",
        ),
    )

    assert failed.kind == "turn_failed"
    assert failed.state == "failed"
    assert failed.title == "Run failed"
    assert interrupted.kind == "turn_interrupted"
    assert interrupted.state == "interrupted"
    assert stopped.kind == "turn_interrupted"
    assert stopped.title == "Interrupted"
    assert direct_interrupted.kind == "turn_interrupted"
    assert direct_interrupted.state == "interrupted"
    assert direct_interrupted.title == "Interrupted"
    assert direct_interrupted.summary == "Turn interrupted"


def test_progress_projection_suppresses_token_usage() -> None:
    items = _project(
        TokenUsage(timestamp="2026-05-06T10:00:01Z", usage={"total_tokens": 10}),
        RunNotice(
            timestamp="2026-05-06T10:00:02Z", kind="progress", message="Still working"
        ),
    )

    assert [item.kind for item in items] == ["notice"]
    assert items[0].event_ids == (2,)


def test_progress_projection_uses_notice_messages_for_titles() -> None:
    items = _project(
        RunNotice(
            timestamp="2026-05-06T10:00:01Z",
            kind="progress",
            message="Starting pytest",
        )
    )

    assert items[0].kind == "notice"
    assert items[0].title == "Starting pytest"
    assert items[0].summary == "Starting pytest"


def test_progress_projection_keeps_approvals_and_notices_interleaved() -> None:
    items = _project(
        RunNotice(
            timestamp="2026-05-06T10:00:01Z", kind="progress", message="Starting"
        ),
        ApprovalRequested(
            timestamp="2026-05-06T10:00:02Z",
            request_id="approval-1",
            description="Allow write",
            context={"scope": "workspace"},
        ),
        RunNotice(timestamp="2026-05-06T10:00:03Z", kind="progress", message="Queued"),
    )

    assert [item.kind for item in items] == ["notice", "approval", "notice"]
    assert [item.event_ids for item in items] == [(1,), (2,), (3,)]


def test_progress_projection_projects_user_input_requests() -> None:
    items = _project(
        UserInputRequested(
            timestamp="2026-05-06T10:00:02Z",
            request_id="question-1",
            description="Which framework?",
            questions=({"id": "framework", "text": "Which framework?"},),
            context={"source": "opencode"},
        )
    )

    assert len(items) == 1
    assert items[0].kind == "user_input"
    assert items[0].state == "waiting"
    assert items[0].title == "User input requested"
    assert items[0].summary == "Which framework?"
    assert items[0].item_id == "progress:user_input:question-1"


def test_progress_projection_merges_streamed_progress_fragments() -> None:
    items = _project(
        RunNotice(timestamp="2026-05-06T10:00:01Z", kind="progress", message="1"),
        RunNotice(timestamp="2026-05-06T10:00:02Z", kind="progress", message="."),
        RunNotice(timestamp="2026-05-06T10:00:03Z", kind="progress", message="2"),
    )

    assert len(items) == 1
    assert items[0].kind == "notice"
    assert items[0].title == "Progress"
    assert items[0].summary == "1.2"
    assert items[0].event_ids == (1, 2, 3)


def test_progress_projection_keeps_specific_progress_notice_titles() -> None:
    items = _project(
        RunNotice(
            timestamp="2026-05-06T10:00:01Z",
            kind="progress",
            message="entered review mode",
        )
    )

    assert items[0].kind == "notice"
    assert items[0].title == "entered review mode"


def test_progress_projection_dedupes_cumulative_thinking_snapshots() -> None:
    items = _project(
        RunNotice(timestamp="2026-05-06T10:00:01Z", kind="thinking", message="Read"),
        RunNotice(
            timestamp="2026-05-06T10:00:02Z", kind="thinking", message="Read files"
        ),
        RunNotice(
            timestamp="2026-05-06T10:00:03Z",
            kind="thinking",
            message="Read files now",
        ),
    )

    assert len(items) == 1
    assert items[0].kind == "assistant_update"
    assert items[0].summary == "Read files now"
    assert items[0].merge_strategy == RUN_EVENT_STREAM_MODE_SNAPSHOT
    assert items[0].event_ids == (1, 2, 3)


def test_progress_projection_dedupes_cumulative_agent_thought_chunk_snapshots() -> None:
    items = _project(
        RunNotice(
            timestamp="2026-05-06T10:00:01Z", kind="thinking", message="The user"
        ),
        RunNotice(
            timestamp="2026-05-06T10:00:02Z",
            kind="thinking",
            message="The user is accessing",
        ),
        RunNotice(
            timestamp="2026-05-06T10:00:03Z",
            kind="thinking",
            message="The user is accessing the dashboard",
        ),
    )

    assert len(items) == 1
    assert items[0].kind == "assistant_update"
    assert items[0].summary == "The user is accessing the dashboard"
    assert items[0].merge_strategy == RUN_EVENT_STREAM_MODE_SNAPSHOT
    assert items[0].event_ids == (1, 2, 3)


def test_high_volume_stream_deltas_reduce_to_bounded_visible_projection() -> None:
    events = [
        ProgressProjectionInput(
            event_id=index,
            timestamp=f"2026-05-06T10:00:{index % 60:02d}Z",
            event=OutputDelta(
                timestamp=f"2026-05-06T10:00:{index % 60:02d}Z",
                content=f"chunk-{index} ",
                delta_type=(
                    RUN_EVENT_DELTA_TYPE_LOG_LINE
                    if index % 100 == 0
                    else RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM
                ),
            ),
        )
        for index in range(1, 1001)
    ]

    items = project_progress_events(events)

    assert len(items) == 1
    assert items[0].kind == "assistant_update"
    assert len(items[0].event_ids) == 1000
    assert items[0].event_ids[0] == 1
    assert items[0].event_ids[-1] == 1000
