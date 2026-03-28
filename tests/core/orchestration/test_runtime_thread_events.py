from __future__ import annotations

from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
    recover_post_completion_outcome,
    terminal_run_event_from_outcome,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadOutcome
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
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


async def test_normalize_runtime_thread_raw_event_surfaces_generic_error_notifications() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "error",
                    "params": {"error": {"message": "Auth required"}},
                }
            },
        ),
        state,
    )

    assert isinstance(output[0], Failed)
    assert output[0].error_message == "Auth required"
    assert state.last_error_message == "Auth required"


async def test_normalize_runtime_thread_raw_event_marks_only_successful_turn_completed() -> (
    None
):
    failed_state = RuntimeThreadRunEventState()
    await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "turn/completed",
                    "params": {"status": "failed"},
                }
            },
        ),
        failed_state,
    )
    assert failed_state.completed_seen is False

    completed_state = RuntimeThreadRunEventState()
    await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "turn/completed",
                    "params": {"status": "completed"},
                }
            },
        ),
        completed_state,
    )
    assert completed_state.completed_seen is True


async def test_normalize_runtime_thread_raw_event_handles_acp_progress_approval_and_completion() -> (
    None
):
    state = RuntimeThreadRunEventState()

    progress = await normalize_runtime_thread_raw_event(
        {
            "method": "prompt/progress",
            "params": {"delta": "hello "},
        },
        state,
    )
    notice = await normalize_runtime_thread_raw_event(
        {
            "method": "prompt/progress",
            "params": {"message": "thinking"},
        },
        state,
    )
    approval = await normalize_runtime_thread_raw_event(
        {
            "method": "permission/requested",
            "params": {
                "requestId": "perm-1",
                "description": "Need approval",
                "context": {"tool": "shell"},
            },
        },
        state,
    )
    final_message = await normalize_runtime_thread_raw_event(
        {
            "method": "prompt/completed",
            "params": {"status": "completed", "finalOutput": "done"},
        },
        state,
    )

    assert isinstance(progress[0], OutputDelta)
    assert progress[0].content == "hello "
    assert isinstance(notice[0], RunNotice)
    assert notice[0].kind == "progress"
    assert notice[0].message == "thinking"
    assert isinstance(approval[0], ApprovalRequested)
    assert approval[0].request_id == "perm-1"
    assert approval[0].context == {"tool": "shell"}
    assert isinstance(final_message[0], OutputDelta)
    assert final_message[0].delta_type == "assistant_message"
    assert final_message[0].content == "done"
    assert state.best_assistant_text() == "done"
    assert state.completed_seen is True


async def test_normalize_runtime_thread_raw_event_handles_request_style_permission_and_decision() -> (
    None
):
    state = RuntimeThreadRunEventState()

    approval = await normalize_runtime_thread_raw_event(
        {
            "id": "perm-1",
            "method": "session/request_permission",
            "params": {
                "turnId": "turn-1",
                "requestId": "perm-1",
                "description": "Need approval",
                "context": {"tool": "shell"},
            },
        },
        state,
    )
    decision = await normalize_runtime_thread_raw_event(
        {
            "method": "permission/decision",
            "params": {
                "turnId": "turn-1",
                "requestId": "perm-1",
                "decision": "decline",
                "description": "Need approval",
            },
        },
        state,
    )

    assert isinstance(approval[0], ApprovalRequested)
    assert approval[0].request_id == "perm-1"
    assert isinstance(decision[0], RunNotice)
    assert decision[0].kind == "approval"
    assert "declined" in decision[0].message.lower()


async def test_normalize_runtime_thread_raw_event_handles_acp_failure_and_usage() -> (
    None
):
    state = RuntimeThreadRunEventState()

    usage = await normalize_runtime_thread_raw_event(
        {
            "method": "token/usage",
            "params": {"usage": {"total_tokens": 42}},
        },
        state,
    )
    failure = await normalize_runtime_thread_raw_event(
        {
            "method": "prompt/failed",
            "params": {"message": "boom"},
        },
        state,
    )

    assert isinstance(usage[0], TokenUsage)
    assert usage[0].usage == {"total_tokens": 42}
    assert isinstance(failure[0], Failed)
    assert failure[0].error_message == "boom"
    assert state.last_error_message == "boom"


async def test_normalize_runtime_thread_raw_event_handles_official_session_updates() -> (
    None
):
    state = RuntimeThreadRunEventState()

    progress = await normalize_runtime_thread_raw_event(
        {
            "message": {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking"},
                    },
                },
            }
        },
        state,
    )
    output = await normalize_runtime_thread_raw_event(
        {
            "message": {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    },
                },
            }
        },
        state,
    )
    usage = await normalize_runtime_thread_raw_event(
        {
            "message": {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "update": {
                        "sessionUpdate": "usage_update",
                        "usage": {"total_tokens": 42},
                    },
                },
            }
        },
        state,
    )

    assert isinstance(progress[0], RunNotice)
    assert progress[0].kind == "progress"
    assert progress[0].message == "thinking"
    assert isinstance(output[0], OutputDelta)
    assert output[0].content == "hello"
    assert isinstance(usage[0], TokenUsage)
    assert usage[0].usage == {"total_tokens": 42}
    assert state.best_assistant_text() == "hello"
    assert state.token_usage == {"total_tokens": 42}


async def test_recover_post_completion_outcome_requires_canonical_final_message() -> (
    None
):
    outcome = RuntimeThreadOutcome(
        status="error",
        assistant_text="",
        error="App-server disconnected",
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )

    partial_only_state = RuntimeThreadRunEventState(
        assistant_stream_text="partial output",
        completed_seen=True,
    )
    assert recover_post_completion_outcome(outcome, partial_only_state) == outcome

    final_message_state = RuntimeThreadRunEventState(
        assistant_stream_text="partial output",
        assistant_message_text="final canonical output",
        completed_seen=True,
    )
    recovered = recover_post_completion_outcome(outcome, final_message_state)
    assert recovered.status == "ok"
    assert recovered.assistant_text == "final canonical output"


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


async def test_terminal_run_event_from_outcome_redacts_unknown_errors() -> None:
    state = RuntimeThreadRunEventState()

    event = terminal_run_event_from_outcome(
        RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error="backend exploded with private detail",
            backend_thread_id="thread-1",
            backend_turn_id="turn-1",
        ),
        state,
    )

    assert isinstance(event, Failed)
    assert event.error_message == "Runtime thread failed"


async def test_normalize_runtime_thread_raw_event_deduplicates_identical_stream_chunks() -> (
    None
):
    state = RuntimeThreadRunEventState()

    await normalize_runtime_thread_raw_event(
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
    await normalize_runtime_thread_raw_event(
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

    assert state.assistant_stream_text == "partial reply"


async def test_normalize_runtime_thread_raw_event_handles_opencode_message_part_updates() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {"type": "text", "text": "OK"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert isinstance(output[0], OutputDelta)
    assert output[0].content == "OK"
    assert state.best_assistant_text() == "OK"


async def test_normalize_runtime_thread_raw_event_maps_opencode_reasoning_parts_to_thinking() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "reason-1",
                                "type": "reasoning",
                                "text": "thinking through the repo",
                            },
                            "delta": {"text": "thinking through the repo"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 1
    assert isinstance(output[0], RunNotice)
    assert output[0].kind == "thinking"
    assert output[0].message == "thinking through the repo"
    assert state.best_assistant_text() == ""


async def test_normalize_runtime_thread_raw_event_maps_opencode_tool_parts_to_tool_calls() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "tool": "bash",
                                "input": "pwd",
                                "state": {"status": "running"},
                            }
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 1
    assert isinstance(output[0], ToolCall)
    assert output[0].tool_name == "bash"
    assert output[0].tool_input == {"input": "pwd"}


async def test_normalize_runtime_thread_raw_event_maps_codex_tool_end_to_tool_result() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "item/toolCall/end",
                    "params": {
                        "name": "shell",
                        "result": {"stdout": "/tmp"},
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 1
    assert isinstance(output[0], ToolResult)
    assert output[0].tool_name == "shell"
    assert output[0].status == "completed"
    assert output[0].result == {"stdout": "/tmp"}


async def test_normalize_runtime_thread_raw_event_maps_opencode_tool_completion_to_tool_result() -> (
    None
):
    state = RuntimeThreadRunEventState(opencode_tool_status={"tool-1": "running"})

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "tool": "bash",
                                "input": "pwd",
                                "state": {"status": "completed", "exitCode": 0},
                            }
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 2
    assert isinstance(output[0], ToolResult)
    assert output[0].tool_name == "bash"
    assert output[0].status == "completed"
    assert isinstance(output[1], OutputDelta)
    assert output[1].content == "exit 0"


async def test_normalize_runtime_thread_raw_event_maps_opencode_patch_parts_to_log_lines() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "patch-1",
                                "type": "patch",
                                "hash": "abc123",
                                "files": ["src/example.py"],
                            }
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 2
    assert all(isinstance(event, OutputDelta) for event in output)
    assert output[0].delta_type == "log_line"
    assert output[0].content == "file update"
    assert output[1].content == "M src/example.py"


async def test_normalize_runtime_thread_raw_event_maps_opencode_usage_parts_to_token_usage() -> (
    None
):
    state = RuntimeThreadRunEventState()

    output = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "usage-1",
                                "type": "usage",
                                "totalTokens": 12,
                                "inputTokens": 3,
                                "outputTokens": 9,
                            }
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 1
    assert isinstance(output[0], TokenUsage)
    assert output[0].usage == {
        "totalTokens": 12,
        "inputTokens": 3,
        "outputTokens": 9,
    }
    assert state.token_usage == output[0].usage


async def test_normalize_runtime_thread_raw_event_ignores_user_message_part_updates() -> (
    None
):
    state = RuntimeThreadRunEventState()

    pending = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "type": "text",
                                "messageID": "user-1",
                                "text": "user prompt",
                            },
                        }
                    },
                }
            },
        ),
        state,
    )
    resolved = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.updated",
                    "params": {
                        "properties": {
                            "info": {"id": "user-1", "role": "user"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert pending == []
    assert resolved == []
    assert state.best_assistant_text() == ""


async def test_normalize_runtime_thread_raw_event_flushes_assistant_message_parts_after_role_update() -> (
    None
):
    state = RuntimeThreadRunEventState()

    pending = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "type": "text",
                                "messageID": "assistant-1",
                                "text": "assistant reply",
                            },
                        }
                    },
                }
            },
        ),
        state,
    )
    resolved = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.updated",
                    "params": {
                        "properties": {
                            "info": {"id": "assistant-1", "role": "assistant"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert pending == []
    assert len(resolved) == 1
    assert isinstance(resolved[0], OutputDelta)
    assert resolved[0].content == "assistant reply"
    assert state.best_assistant_text() == "assistant reply"
