from __future__ import annotations

from codex_autorunner.core.orchestration.progress_projection import (
    ProgressProjectionInput,
    project_progress_events,
)
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Failed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
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
    failed, interrupted = _project(
        Failed(timestamp="2026-05-06T10:00:01Z", error_message="boom"),
        Failed(timestamp="2026-05-06T10:00:02Z", error_message="cancelled by user"),
    )

    assert failed.kind == "turn_failed"
    assert failed.state == "failed"
    assert failed.title == "Run failed"
    assert interrupted.kind == "turn_interrupted"
    assert interrupted.state == "interrupted"


def test_progress_projection_suppresses_token_usage() -> None:
    items = _project(
        TokenUsage(timestamp="2026-05-06T10:00:01Z", usage={"total_tokens": 10}),
        RunNotice(
            timestamp="2026-05-06T10:00:02Z", kind="progress", message="Still working"
        ),
    )

    assert [item.kind for item in items] == ["notice"]
    assert items[0].event_ids == (2,)


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
