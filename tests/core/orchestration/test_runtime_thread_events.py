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


async def test_normalize_runtime_thread_raw_event_emits_progress_for_session_status() -> (
    None
):
    working_state = RuntimeThreadRunEventState()
    working = await normalize_runtime_thread_raw_event(
        {
            "method": "session.status",
            "params": {"status": {"type": "running"}},
        },
        working_state,
    )
    assert isinstance(working[0], RunNotice)
    assert working[0].kind == "progress"
    assert working[0].message == "agent running"
    assert working_state.completed_seen is False

    idle_like_state = RuntimeThreadRunEventState()
    idle_like = await normalize_runtime_thread_raw_event(
        {
            "method": "session.status",
            "params": {"status": {"type": "idle"}},
        },
        idle_like_state,
    )
    assert idle_like == []
    assert idle_like_state.completed_seen is True

    empty_state = RuntimeThreadRunEventState()
    empty = await normalize_runtime_thread_raw_event(
        {
            "method": "session.status",
            "params": {"status": {}},
        },
        empty_state,
    )
    assert empty == []
    assert empty_state.completed_seen is False

    properties_busy_state = RuntimeThreadRunEventState()
    properties_busy = await normalize_runtime_thread_raw_event(
        {
            "method": "session.status",
            "params": {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_abc123",
                    "status": {"type": "busy"},
                },
            },
        },
        properties_busy_state,
    )
    assert isinstance(properties_busy[0], RunNotice)
    assert properties_busy[0].kind == "progress"
    assert properties_busy[0].message == "agent busy"
    assert properties_busy_state.completed_seen is False

    properties_idle_state = RuntimeThreadRunEventState()
    properties_idle = await normalize_runtime_thread_raw_event(
        {
            "method": "session.status",
            "params": {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_abc123",
                    "status": {"type": "idle"},
                },
            },
        },
        properties_idle_state,
    )
    assert properties_idle == []
    assert properties_idle_state.completed_seen is True


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


async def test_normalize_runtime_thread_raw_event_preserves_session_update_object_shapes() -> (
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
                        "content": {"status": "thinking"},
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
                        "content": {"delta": "hello"},
                    },
                },
            }
        },
        state,
    )
    output_with_output_key = await normalize_runtime_thread_raw_event(
        {
            "message": {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"output": " world"},
                    },
                },
            }
        },
        state,
    )

    assert len(progress) == 1
    assert isinstance(progress[0], RunNotice)
    assert progress[0].message == "thinking"
    assert len(output) == 1
    assert isinstance(output[0], OutputDelta)
    assert output[0].content == "hello"
    assert len(output_with_output_key) == 1
    assert isinstance(output_with_output_key[0], OutputDelta)
    assert output_with_output_key[0].content == " world"


async def test_normalize_runtime_thread_raw_event_handles_official_session_update_content_parts() -> (
    None
):
    state = RuntimeThreadRunEventState()

    thinking = await normalize_runtime_thread_raw_event(
        {
            "message": {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": [
                            {"type": "text", "text": "thinking"},
                            {"type": "message", "message": " more"},
                        ],
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
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "output_text", "text": " world"},
                        ],
                    },
                },
            }
        },
        state,
    )

    assert len(thinking) == 1
    assert isinstance(thinking[0], RunNotice)
    assert thinking[0].message == "thinking more"
    assert len(output) == 1
    assert isinstance(output[0], OutputDelta)
    assert output[0].content == "hello world"


async def test_recover_post_completion_outcome_uses_streamed_output_after_completion() -> (
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
    recovered_from_stream = recover_post_completion_outcome(outcome, partial_only_state)
    assert recovered_from_stream.status == "ok"
    assert recovered_from_stream.assistant_text == "partial output"

    final_message_state = RuntimeThreadRunEventState(
        assistant_stream_text="partial output",
        assistant_message_text="final canonical output",
        completed_seen=True,
    )
    recovered = recover_post_completion_outcome(outcome, final_message_state)
    assert recovered.status == "ok"
    assert recovered.assistant_text == "final canonical output"


async def test_recover_post_completion_outcome_recovers_interrupted_with_final_message() -> (
    None
):
    outcome = RuntimeThreadOutcome(
        status="interrupted",
        assistant_text="",
        error="Runtime thread interrupted",
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )

    state = RuntimeThreadRunEventState(
        assistant_message_text="final canonical output",
        completed_seen=True,
    )

    recovered = recover_post_completion_outcome(outcome, state)
    assert recovered.status == "ok"
    assert recovered.assistant_text == "final canonical output"


async def test_recover_post_completion_outcome_requires_completion_signal() -> None:
    outcome = RuntimeThreadOutcome(
        status="error",
        assistant_text="",
        error="App-server disconnected",
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )

    state = RuntimeThreadRunEventState(
        assistant_stream_text="partial output",
        completed_seen=False,
    )

    assert recover_post_completion_outcome(outcome, state) == outcome


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


async def test_normalize_runtime_thread_raw_event_handles_opencode_message_part_deltas_with_direct_ids() -> (
    None
):
    state = RuntimeThreadRunEventState()

    await normalize_runtime_thread_raw_event(
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
    await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "part-1",
                                "type": "text",
                                "messageID": "assistant-1",
                                "text": "Hello",
                            },
                            "delta": {"text": "Hello"},
                        }
                    },
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
                    "method": "message.part.delta",
                    "params": {
                        "properties": {
                            "partID": "part-1",
                            "messageID": "assistant-1",
                            "delta": {"text": " world"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 1
    assert isinstance(output[0], OutputDelta)
    assert output[0].content == " world"
    assert state.best_assistant_text() == "Hello world"


async def test_normalize_runtime_thread_raw_event_handles_string_delta_in_delta_only_text_path() -> (
    None
):
    state = RuntimeThreadRunEventState()

    await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.updated",
                    "params": {
                        "properties": {
                            "info": {"id": "assistant-2", "role": "assistant"},
                        }
                    },
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
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "part-2",
                                "type": "text",
                                "messageID": "assistant-2",
                            },
                            "delta": {"text": "Hello"},
                        }
                    },
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
                    "method": "message.part.delta",
                    "params": {
                        "properties": {
                            "partID": "part-2",
                            "messageID": "assistant-2",
                            "delta": " world",
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(output) == 1
    assert isinstance(output[0], OutputDelta)
    assert output[0].content == " world"
    assert state.best_assistant_text() == "Hello world"


async def test_normalize_runtime_thread_raw_event_uses_part_type_memory_for_reasoning_deltas() -> (
    None
):
    state = RuntimeThreadRunEventState()

    first = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {"id": "reason-1", "type": "reasoning"},
                            "delta": {"text": "thinking"},
                        }
                    },
                }
            },
        ),
        state,
    )
    second = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.delta",
                    "params": {
                        "properties": {
                            "partID": "reason-1",
                            "delta": {"text": " more"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(first) == 1
    assert isinstance(first[0], RunNotice)
    assert first[0].message == "thinking"
    assert len(second) == 1
    assert isinstance(second[0], RunNotice)
    assert second[0].message == "thinking more"
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


async def test_normalize_runtime_thread_raw_event_reads_top_level_info_for_role_updates() -> (
    None
):
    state = RuntimeThreadRunEventState()

    pending = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.delta",
                    "params": {
                        "properties": {
                            "partID": "part-legacy",
                            "messageID": "assistant-legacy",
                            "delta": "hello",
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
                        "info": {"id": "assistant-legacy", "role": "assistant"},
                    },
                }
            },
        ),
        state,
    )

    assert pending == []
    assert len(resolved) == 1
    assert isinstance(resolved[0], OutputDelta)
    assert resolved[0].content == "hello"
    assert state.best_assistant_text() == "hello"


async def test_message_delta_with_reasoning_part_type_is_dropped() -> None:
    """message.delta carrying reasoning part type must not become OutputDelta."""
    state = RuntimeThreadRunEventState()

    events = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.delta",
                    "params": {
                        "properties": {
                            "delta": {"text": "thinking about the problem"},
                            "part": {"id": "r1", "type": "reasoning"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert events == []
    assert state.assistant_stream_text == ""
    assert state.best_assistant_text() == ""


async def test_message_delta_with_text_part_type_is_kept() -> None:
    """message.delta carrying text part type should produce OutputDelta."""
    state = RuntimeThreadRunEventState()

    events = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.delta",
                    "params": {
                        "properties": {
                            "delta": {"text": "the answer"},
                            "part": {"id": "t1", "type": "text"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(events) == 1
    assert isinstance(events[0], OutputDelta)
    assert events[0].content == "the answer"
    assert state.best_assistant_text() == "the answer"


async def test_message_delta_without_part_type_is_kept() -> None:
    """message.delta without part metadata should still work (legacy)."""
    state = RuntimeThreadRunEventState()

    events = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.delta",
                    "params": {
                        "properties": {
                            "delta": {"text": "hello world"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert len(events) == 1
    assert isinstance(events[0], OutputDelta)
    assert events[0].content == "hello world"
    assert state.best_assistant_text() == "hello world"


async def test_message_delta_reasoning_does_not_pollute_stream_when_paired_with_part_events() -> (
    None
):
    """Regression: reasoning arriving as both message.delta and message.part.delta
    should produce exactly one RunNotice(thinking) and not contaminate assistant_stream_text.
    """
    state = RuntimeThreadRunEventState()

    delta_events = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.delta",
                    "params": {
                        "properties": {
                            "delta": {"text": "Let me think"},
                            "part": {"id": "r1", "type": "reasoning"},
                        }
                    },
                }
            },
        ),
        state,
    )

    part_events = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.delta",
                    "params": {
                        "properties": {
                            "part": {
                                "id": "r1",
                                "type": "reasoning",
                                "text": "Let me think",
                            },
                            "delta": {"text": "Let me think"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert delta_events == []
    assert len(part_events) == 1
    assert isinstance(part_events[0], RunNotice)
    assert part_events[0].kind == "thinking"
    assert state.assistant_stream_text == ""
    assert state.best_assistant_text() == ""


async def test_message_delta_uses_cached_part_type_for_reasoning() -> None:
    """message.delta referencing a known reasoning part ID (without inline type)
    should still be dropped."""
    state = RuntimeThreadRunEventState()

    await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "properties": {
                            "part": {"id": "r1", "type": "reasoning"},
                            "delta": {"text": "initial"},
                        }
                    },
                }
            },
        ),
        state,
    )

    events = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.delta",
                    "params": {
                        "properties": {
                            "delta": {"text": "more thinking"},
                            "part": {"id": "r1"},
                        }
                    },
                }
            },
        ),
        state,
    )

    assert events == []
    assert state.assistant_stream_text == ""
