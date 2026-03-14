from __future__ import annotations

from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
    terminal_run_event_from_outcome,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadOutcome
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
)
from codex_autorunner.core.sse import format_sse


async def test_normalize_runtime_thread_raw_event_handles_codex_app_server_updates() -> (
    None
):
    state = RuntimeThreadRunEventState()

    thinking = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": "reason-1", "delta": "thinking step"},
                }
            },
        ),
        state,
    )
    tool = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "item/toolCall/start",
                    "params": {
                        "item": {"toolCall": {"name": "shell", "input": {"cmd": "pwd"}}}
                    },
                }
            },
        ),
        state,
    )
    approval = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": ["rm", "-rf", "/tmp/example"]},
                }
            },
        ),
        state,
    )
    usage = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "thread/tokenUsage/updated",
                    "params": {"tokenUsage": {"total_tokens": 12}},
                }
            },
        ),
        state,
    )
    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "item/agentMessage/delta",
                    "params": {"delta": "partial reply"},
                }
            },
        ),
        state,
    )

    assert isinstance(thinking[0], RunNotice)
    assert thinking[0].kind == "thinking"
    assert thinking[0].message == "thinking step"
    assert isinstance(tool[0], ToolCall)
    assert tool[0].tool_name == "shell"
    assert isinstance(approval[0], ApprovalRequested)
    assert approval[0].description == "rm -rf /tmp/example"
    assert isinstance(usage[0], TokenUsage)
    assert usage[0].usage == {"total_tokens": 12}
    assert isinstance(output[0], OutputDelta)
    assert output[0].content == "partial reply"
    assert state.best_assistant_text() == "partial reply"


async def test_terminal_run_event_from_outcome_uses_streamed_fallback_text() -> None:
    state = RuntimeThreadRunEventState(assistant_stream_text="streamed fallback")

    event = terminal_run_event_from_outcome(
        RuntimeThreadOutcome(
            status="ok",
            assistant_text="",
            error=None,
            backend_thread_id="thread-1",
            backend_turn_id="turn-1",
        ),
        state,
    )

    assert isinstance(event, Completed)
    assert event.final_message == "streamed fallback"
