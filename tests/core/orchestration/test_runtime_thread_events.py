from __future__ import annotations

from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.core.orchestration.runtime_thread_events import (
    DECODE_FAILURE_REASON_EMPTY_METHOD,
    DECODE_FAILURE_REASON_REGISTRY_MISS,
    DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE,
    RuntimeThreadRunEventState,
    completion_source_from_outcome,
    decode_runtime_raw_messages,
    normalize_runtime_progress_event,
    normalize_runtime_thread_raw_event,
    note_run_event_state,
    raw_event_content_summary,
    raw_event_message,
    raw_event_method,
    raw_event_session_update,
    recover_post_completion_outcome,
    runtime_trace_fields,
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


async def test_normalize_runtime_thread_raw_event_shared_lifecycle_corpus() -> None:
    for case in load_acp_lifecycle_corpus():
        state = RuntimeThreadRunEventState()
        raw = dict(case["raw"])
        expected = dict(case["expected"])

        events = await normalize_runtime_thread_raw_event(raw, state)

        if expected["normalized_kind"] == "turn_terminal":
            if expected["runtime_terminal_status"] == "ok":
                assert state.completed_seen is True
                if expected["assistant_text"] and raw["method"] in {
                    "prompt/completed",
                    "turn/completed",
                }:
                    assert state.best_assistant_text() == expected["assistant_text"]
            elif expected["runtime_terminal_status"] == "error":
                assert isinstance(events[0], Failed)
                assert events[0].error_message == expected["error_message"]
                assert state.last_error_message == expected["error_message"]
        elif expected["normalized_kind"] == "output_delta":
            if expected.get("message_phase") == "commentary":
                assert isinstance(events[0], RunNotice)
                assert events[0].kind == "commentary"
                assert events[0].message == expected["output_delta"]
                assert events[0].data.get("already_streamed") is expected.get(
                    "already_streamed", False
                )
                assert state.best_assistant_text() == ""
            else:
                assert isinstance(events[0], OutputDelta)
                assert events[0].content == expected["output_delta"]
                assert state.best_assistant_text() == expected["output_delta"]
        elif expected["normalized_kind"] == "progress":
            if (
                raw["method"] in {"session.status", "session/status"}
                and expected["session_status"] == "idle"
            ):
                assert events == []
                assert state.completed_seen is True
            else:
                assert isinstance(events[0], RunNotice)
                expected_message = (
                    f"agent {expected['progress_message']}"
                    if raw["method"] in {"session.status", "session/status"}
                    else expected["progress_message"]
                )
                assert events[0].message == expected_message
        elif expected["normalized_kind"] == "permission_requested":
            assert isinstance(events[0], ApprovalRequested)
            assert events[0].request_id == expected["permission_request_id"]


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
    assert state.last_runtime_method == "item/agentMessage/delta"
    assert isinstance(state.last_progress_at, str)


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
    assert state.last_runtime_method == "error"
    assert isinstance(state.last_progress_at, str)


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
    assert working_state.last_runtime_method == "session.status"
    assert isinstance(working_state.last_progress_at, str)

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


async def test_normalize_runtime_thread_raw_event_maps_official_commentary_update_to_notice() -> (
    None
):
    state = RuntimeThreadRunEventState()

    events = await normalize_runtime_thread_raw_event(
        {
            "message": {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "phase": "commentary",
                        "alreadyStreamed": True,
                        "content": [
                            {"type": "text", "text": "draft"},
                            {"type": "output_text", "text": " plan"},
                        ],
                    },
                },
            }
        },
        state,
    )

    assert len(events) == 1
    assert isinstance(events[0], RunNotice)
    assert events[0].kind == "commentary"
    assert events[0].message == "draft plan"
    assert events[0].data == {"already_streamed": True}
    assert state.best_assistant_text() == ""


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


async def test_normalize_runtime_thread_raw_event_reads_agent_message_item_on_message_completed() -> (
    None
):
    state = RuntimeThreadRunEventState()

    resolved = await normalize_runtime_thread_raw_event(
        format_sse(
            "app-server",
            {
                "message": {
                    "method": "message.completed",
                    "params": {
                        "info": {"id": "assistant-item", "role": "assistant"},
                        "item": {
                            "type": "agentMessage",
                            "text": "final reply from item",
                        },
                    },
                }
            },
        ),
        state,
    )

    assert len(resolved) == 1
    assert isinstance(resolved[0], OutputDelta)
    assert resolved[0].content == "final reply from item"
    assert state.best_assistant_text() == "final reply from item"


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


class TestCrossBackendUsageParity:
    """Usage extraction should produce identical TokenUsage events across backends."""

    async def test_codex_token_usage_produces_token_usage_event(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"usage": {"totalTokens": 100}}},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert events[0].usage == {"totalTokens": 100}
        assert state.token_usage == {"totalTokens": 100}

    async def test_opencode_turn_token_usage_produces_token_usage_event(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "turn/tokenUsage", "params": {"usage": {"totalTokens": 100}}},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert events[0].usage == {"totalTokens": 100}
        assert state.token_usage == {"totalTokens": 100}

    async def test_session_update_usage_update_produces_token_usage_event(
        self,
    ) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "message": {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "usage_update",
                            "usage": {"totalTokens": 100},
                        }
                    },
                }
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert events[0].usage == {"totalTokens": 100}
        assert state.token_usage == {"totalTokens": 100}

    async def test_thread_token_usage_updated_produces_token_usage_event(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "thread/tokenUsage/updated",
                "params": {"tokenUsage": {"totalTokens": 100}},
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert events[0].usage == {"totalTokens": 100}
        assert state.token_usage == {"totalTokens": 100}

    async def test_opencode_usage_part_produces_token_usage_event(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
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
                                    "totalTokens": 100,
                                    "inputTokens": 60,
                                    "outputTokens": 40,
                                }
                            }
                        },
                    }
                },
            ),
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert events[0].usage == {
            "totalTokens": 100,
            "inputTokens": 60,
            "outputTokens": 40,
        }
        assert state.token_usage == {
            "totalTokens": 100,
            "inputTokens": 60,
            "outputTokens": 40,
        }

    async def test_usage_alias_keys_produce_identical_events(self) -> None:
        canonical_state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"usage": {"totalTokens": 42}}},
            canonical_state,
        )

        snake_state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"usage": {"total_tokens": 42}}},
            snake_state,
        )

        assert canonical_state.token_usage == {"totalTokens": 42}
        assert snake_state.token_usage == {"total_tokens": 42}


class TestCrossBackendToolParity:
    """Tool call/result should produce equivalent events across Codex and OpenCode."""

    async def test_codex_item_tool_call_start_and_end(self) -> None:
        state = RuntimeThreadRunEventState()

        start = await normalize_runtime_thread_raw_event(
            {
                "method": "item/toolCall/start",
                "params": {
                    "item": {"toolCall": {"name": "shell", "input": {"cmd": "pwd"}}}
                },
            },
            state,
        )
        end = await normalize_runtime_thread_raw_event(
            {
                "method": "item/toolCall/end",
                "params": {"name": "shell", "result": {"stdout": "/tmp"}},
            },
            state,
        )

        assert isinstance(start[0], ToolCall)
        assert start[0].tool_name == "shell"
        assert isinstance(end[0], ToolResult)
        assert end[0].tool_name == "shell"
        assert end[0].status == "completed"
        assert end[0].result == {"stdout": "/tmp"}

    async def test_opencode_tool_part_running_then_completed(self) -> None:
        state = RuntimeThreadRunEventState()

        start = await normalize_runtime_thread_raw_event(
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
        end = await normalize_runtime_thread_raw_event(
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
                                    "state": {"status": "completed", "exitCode": 0},
                                }
                            }
                        },
                    }
                },
            ),
            state,
        )

        assert isinstance(start[0], ToolCall)
        assert start[0].tool_name == "bash"
        assert isinstance(end[0], ToolResult)
        assert end[0].tool_name == "bash"
        assert end[0].status == "completed"

    async def test_opencode_tool_part_error_status(self) -> None:
        state = RuntimeThreadRunEventState(opencode_tool_status={"tool-err": "running"})

        events = await normalize_runtime_thread_raw_event(
            format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.part.updated",
                        "params": {
                            "properties": {
                                "part": {
                                    "id": "tool-err",
                                    "type": "tool",
                                    "tool": "bash",
                                    "state": {
                                        "status": "failed",
                                        "error": "non-zero exit",
                                    },
                                }
                            }
                        },
                    }
                },
            ),
            state,
        )

        assert isinstance(events[0], ToolResult)
        assert events[0].tool_name == "bash"
        assert events[0].status == "failed"
        assert isinstance(events[1], OutputDelta)
        assert "non-zero exit" in events[1].content


class TestCrossBackendCommentaryFiltering:
    """Commentary/reasoning must not leak into final assistant answer."""

    async def test_codex_commentary_agent_message_becomes_notice(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "thinking about approach",
                    }
                },
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "commentary"
        assert events[0].message == "thinking about approach"
        assert state.best_assistant_text() == ""

    async def test_codex_non_commentary_agent_message_kept(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "final answer"}},
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "final answer"
        assert state.best_assistant_text() == "final answer"

    async def test_opencode_commentary_message_completed_becomes_notice(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "message.completed",
                "params": {
                    "phase": "commentary",
                    "properties": {
                        "info": {"id": "msg-1", "role": "assistant"},
                    },
                    "text": "internal commentary",
                },
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "commentary"
        assert events[0].message == "internal commentary"
        assert state.best_assistant_text() == ""

    async def test_opencode_reasoning_part_never_leaks_to_stream(self) -> None:
        state = RuntimeThreadRunEventState()

        await normalize_runtime_thread_raw_event(
            format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.part.updated",
                        "params": {
                            "properties": {
                                "part": {
                                    "id": "r1",
                                    "type": "reasoning",
                                    "text": "let me think deeply",
                                },
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
                                    "type": "text",
                                    "text": "the actual answer",
                                },
                            }
                        },
                    }
                },
            ),
            state,
        )

        assert state.best_assistant_text() == "the actual answer"
        assert state.assistant_stream_text == "the actual answer"

    async def test_opencode_assistant_stream_phase_commentary_becomes_notice(
        self,
    ) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "prompt/output",
                "params": {"delta": "commentary text", "phase": "commentary"},
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "commentary"
        assert events[0].message == "commentary text"
        assert state.assistant_stream_text == ""

    async def test_prompt_message_commentary_becomes_notice(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "prompt/message",
                "params": {
                    "text": "reasoning about the problem",
                    "phase": "commentary",
                },
            },
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "commentary"
        assert events[0].message == "reasoning about the problem"
        assert state.best_assistant_text() == ""


class TestCrossBackendApprovalParity:
    """Approval routing should produce equivalent events across methods."""

    async def test_permission_requested_produces_approval(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
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

        assert isinstance(events[0], ApprovalRequested)
        assert events[0].request_id == "perm-1"
        assert events[0].description == "Need approval"

    async def test_session_request_permission_produces_approval(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "session/request_permission",
                "params": {
                    "requestId": "perm-2",
                    "description": "Need approval",
                    "context": {"tool": "shell"},
                },
            },
            state,
        )

        assert isinstance(events[0], ApprovalRequested)
        assert events[0].request_id == "perm-2"
        assert events[0].description == "Need approval"

    async def test_command_execution_request_approval_produces_approval(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "item/commandExecution/requestApproval",
                "params": {"command": ["rm", "-rf", "/tmp/example"]},
            },
            state,
        )

        assert isinstance(events[0], ApprovalRequested)
        assert "rm -rf /tmp/example" in events[0].description

    async def test_file_change_request_approval_produces_approval(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {
                "method": "item/fileChange/requestApproval",
                "params": {"files": ["src/main.py", "README.md"]},
            },
            state,
        )

        assert isinstance(events[0], ApprovalRequested)
        assert "src/main.py" in events[0].description
        assert "README.md" in events[0].description


class TestCrossBackendCompletionParity:
    """Completion/failure events should have consistent semantics."""

    async def test_prompt_completed_marks_completed_seen(self) -> None:
        state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {
                "method": "prompt/completed",
                "params": {"status": "completed", "finalOutput": "done"},
            },
            state,
        )
        assert state.completed_seen is True

    async def test_turn_completed_marks_completed_seen(self) -> None:
        state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {"method": "turn/completed", "params": {"status": "completed"}},
            state,
        )
        assert state.completed_seen is True

    async def test_session_idle_marks_completed_seen(self) -> None:
        state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {"method": "session.idle", "params": {}},
            state,
        )
        assert state.completed_seen is True

    async def test_prompt_failed_produces_failed_event(self) -> None:
        state = RuntimeThreadRunEventState()
        events = await normalize_runtime_thread_raw_event(
            {"method": "prompt/failed", "params": {"message": "boom"}},
            state,
        )
        assert isinstance(events[0], Failed)
        assert events[0].error_message == "boom"
        assert state.completed_seen is False

    async def test_turn_failed_produces_failed_event(self) -> None:
        state = RuntimeThreadRunEventState()
        events = await normalize_runtime_thread_raw_event(
            {"method": "turn/failed", "params": {"message": "crash"}},
            state,
        )
        assert isinstance(events[0], Failed)
        assert events[0].error_message == "crash"
        assert state.completed_seen is False

    async def test_prompt_cancelled_does_not_mark_completed(self) -> None:
        state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {"method": "prompt/cancelled", "params": {"status": "cancelled"}},
            state,
        )
        assert state.completed_seen is False

    async def test_turn_cancelled_does_not_mark_completed(self) -> None:
        state = RuntimeThreadRunEventState()
        await normalize_runtime_thread_raw_event(
            {"method": "turn/cancelled", "params": {"status": "cancelled"}},
            state,
        )
        assert state.completed_seen is False


class TestProtocolDriftAliases:
    """Different key aliases for the same semantic data should produce identical results."""

    async def test_tool_name_from_tool_vs_name_key(self) -> None:
        tool_state = RuntimeThreadRunEventState()
        events_a = await normalize_runtime_thread_raw_event(
            format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.part.updated",
                        "params": {
                            "properties": {
                                "part": {
                                    "id": "t1",
                                    "type": "tool",
                                    "tool": "bash",
                                    "state": {"status": "running"},
                                }
                            }
                        },
                    }
                },
            ),
            tool_state,
        )

        name_state = RuntimeThreadRunEventState()
        events_b = await normalize_runtime_thread_raw_event(
            format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.part.updated",
                        "params": {
                            "properties": {
                                "part": {
                                    "id": "t1",
                                    "type": "tool",
                                    "name": "bash",
                                    "state": {"status": "running"},
                                }
                            }
                        },
                    }
                },
            ),
            name_state,
        )

        assert len(events_a) == 1
        assert len(events_b) == 1
        assert events_a[0].tool_name == events_b[0].tool_name == "bash"

    async def test_tool_id_from_callID_vs_id(self) -> None:
        callid_state = RuntimeThreadRunEventState()
        events_a = await normalize_runtime_thread_raw_event(
            format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.part.updated",
                        "params": {
                            "properties": {
                                "part": {
                                    "callID": "c1",
                                    "type": "tool",
                                    "tool": "bash",
                                    "state": {"status": "running"},
                                }
                            }
                        },
                    }
                },
            ),
            callid_state,
        )

        id_state = RuntimeThreadRunEventState()
        events_b = await normalize_runtime_thread_raw_event(
            format_sse(
                "app-server",
                {
                    "message": {
                        "method": "message.part.updated",
                        "params": {
                            "properties": {
                                "part": {
                                    "id": "c1",
                                    "type": "tool",
                                    "tool": "bash",
                                    "state": {"status": "running"},
                                }
                            }
                        },
                    }
                },
            ),
            id_state,
        )

        assert len(events_a) == 1
        assert len(events_b) == 1
        assert (
            callid_state.opencode_tool_status.keys()
            == id_state.opencode_tool_status.keys()
        )

    async def test_tool_input_from_cmd_vs_command_vs_input(self) -> None:
        for key in ("input", "command", "cmd"):
            state = RuntimeThreadRunEventState()
            events = await normalize_runtime_thread_raw_event(
                format_sse(
                    "app-server",
                    {
                        "message": {
                            "method": "message.part.updated",
                            "params": {
                                "properties": {
                                    "part": {
                                        "id": f"tool-{key}",
                                        "type": "tool",
                                        "tool": "bash",
                                        key: "pwd",
                                        "state": {"status": "running"},
                                    }
                                }
                            },
                        }
                    },
                ),
                state,
            )
            assert len(events) == 1
            assert isinstance(events[0], ToolCall)
            assert key in events[0].tool_input
            assert events[0].tool_input[key] == "pwd"

    async def test_usage_from_usage_vs_tokenUsage_key(self) -> None:
        usage_state = RuntimeThreadRunEventState()
        events_a = await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"usage": {"totalTokens": 42}}},
            usage_state,
        )

        token_usage_state = RuntimeThreadRunEventState()
        events_b = await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"tokenUsage": {"totalTokens": 42}}},
            token_usage_state,
        )

        assert len(events_a) == 1
        assert len(events_b) == 1
        assert events_a[0].usage == events_b[0].usage == {"totalTokens": 42}

    async def test_tool_args_vs_arguments_vs_params_key(self) -> None:
        for key in ("args", "arguments", "params"):
            state = RuntimeThreadRunEventState()
            events = await normalize_runtime_thread_raw_event(
                format_sse(
                    "app-server",
                    {
                        "message": {
                            "method": "message.part.updated",
                            "params": {
                                "properties": {
                                    "part": {
                                        "id": f"tool-{key}",
                                        "type": "tool",
                                        "tool": "bash",
                                        key: {"command": "ls"},
                                        "state": {"status": "running"},
                                    }
                                }
                            },
                        }
                    },
                ),
                state,
            )
            assert len(events) == 1
            assert isinstance(events[0], ToolCall)
            assert events[0].tool_input == {"command": "ls"}


class TestRecoveryBehavior:
    """Recovery semantics should be bounded and evidence-based."""

    async def test_recover_post_completion_requires_all_three_conditions(self) -> None:
        from codex_autorunner.core.orchestration.runtime_threads import (
            RuntimeThreadOutcome,
        )

        outcome_error = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error="disconnected",
            backend_thread_id="t1",
            backend_turn_id="turn-1",
        )

        no_completion = recover_post_completion_outcome(
            outcome_error,
            RuntimeThreadRunEventState(
                assistant_stream_text="partial", completed_seen=False
            ),
        )
        assert no_completion.status == "error"

        no_text = recover_post_completion_outcome(
            outcome_error,
            RuntimeThreadRunEventState(completed_seen=True),
        )
        assert no_text.status == "error"

        already_ok = RuntimeThreadOutcome(
            status="ok",
            assistant_text="done",
            error=None,
            backend_thread_id="t1",
            backend_turn_id="turn-1",
        )
        already_ok_result = recover_post_completion_outcome(
            already_ok,
            RuntimeThreadRunEventState(completed_seen=True),
        )
        assert already_ok_result.status == "ok"
        assert already_ok_result.assistant_text == "done"

    async def test_completed_seen_set_only_on_successful_completion(self) -> None:
        for method, status, expected in [
            ("prompt/completed", "completed", True),
            ("turn/completed", "completed", True),
            ("prompt/completed", "failed", False),
            ("turn/completed", "failed", False),
            ("prompt/cancelled", "cancelled", False),
            ("turn/cancelled", "cancelled", False),
            ("session.idle", "", True),
            ("session.status", "", False),
        ]:
            state = RuntimeThreadRunEventState()
            params = {"status": status} if status else {}
            await normalize_runtime_thread_raw_event(
                {"method": method, "params": params},
                state,
            )
            assert (
                state.completed_seen is expected
            ), f"method={method} status={status} expected completed_seen={expected}"


class TestRawEventAccessors:
    def test_raw_event_message_returns_nested_message(self) -> None:
        raw = {"message": {"method": "session/update", "params": {"a": 1}}}
        assert raw_event_message(raw) == {
            "method": "session/update",
            "params": {"a": 1},
        }

    def test_raw_event_message_returns_top_level_when_no_message_key(self) -> None:
        raw = {"method": "turn/completed", "params": {}}
        assert raw_event_message(raw) == raw

    def test_raw_event_message_returns_empty_for_non_dict(self) -> None:
        assert raw_event_message("not a dict") == {}
        assert raw_event_message(None) == {}

    def test_raw_event_message_returns_top_level_when_message_is_not_dict(self) -> None:
        raw = {"message": "string", "method": "x"}
        assert raw_event_message(raw) == raw

    def test_raw_event_method_extracts_from_nested_message(self) -> None:
        raw = {"message": {"method": "session/update"}}
        assert raw_event_method(raw) == "session/update"

    def test_raw_event_method_returns_empty_for_missing(self) -> None:
        assert raw_event_method({}) == ""
        assert raw_event_method(None) == ""

    def test_raw_event_session_update_extracts_update(self) -> None:
        raw = {
            "message": {
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "agent_message_chunk"}},
            }
        }
        assert raw_event_session_update(raw) == {"sessionUpdate": "agent_message_chunk"}

    def test_raw_event_session_update_returns_empty_when_no_update(self) -> None:
        raw = {"message": {"method": "session/update", "params": {}}}
        assert raw_event_session_update(raw) == {}

    def test_raw_event_content_summary_describes_session_update(self) -> None:
        raw = {
            "message": {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "output_text", "text": "world"},
                        ],
                    }
                },
            }
        }
        summary = raw_event_content_summary(raw)
        assert summary["session_update_kind"] == "agent_message_chunk"
        assert summary["content_kind"] == "list"
        assert summary["content_part_count"] == 2
        assert summary["content_part_types"] == ("text", "output_text")

    def test_raw_event_content_summary_handles_missing_content(self) -> None:
        raw = {
            "message": {
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "agent_thought_chunk"}},
            }
        }
        summary = raw_event_content_summary(raw)
        assert summary["session_update_kind"] == "agent_thought_chunk"
        assert summary["content_kind"] == "missing"
        assert summary["content_part_count"] is None


class TestNoteRunEventState:
    def test_note_run_event_state_tracks_stream_text(self) -> None:
        state = RuntimeThreadRunEventState()
        note_run_event_state(
            state,
            OutputDelta(
                timestamp="2026-01-01T00:00:00Z",
                content="hello ",
                delta_type="assistant_stream",
            ),
        )
        assert state.assistant_stream_text == "hello "
        assert state.last_progress_at is not None

    def test_note_run_event_state_tracks_message_text(self) -> None:
        state = RuntimeThreadRunEventState()
        note_run_event_state(
            state,
            OutputDelta(
                timestamp="2026-01-01T00:00:00Z",
                content="final",
                delta_type="assistant_message",
            ),
        )
        assert state.assistant_message_text == "final"

    def test_note_run_event_state_tracks_token_usage(self) -> None:
        state = RuntimeThreadRunEventState()
        note_run_event_state(
            state, TokenUsage(timestamp="2026-01-01T00:00:00Z", usage={"total": 42})
        )
        assert state.token_usage == {"total": 42}

    def test_note_run_event_state_marks_completed(self) -> None:
        state = RuntimeThreadRunEventState()
        note_run_event_state(
            state,
            Completed(timestamp="2026-01-01T00:00:00Z", final_message="done"),
        )
        assert state.completed_seen is True
        assert state.assistant_message_text == "done"

    def test_note_run_event_state_tracks_error(self) -> None:
        state = RuntimeThreadRunEventState()
        note_run_event_state(
            state, Failed(timestamp="2026-01-01T00:00:00Z", error_message="boom")
        )
        assert state.last_error_message == "boom"


async def test_normalize_runtime_progress_event_passes_through_typed_events() -> None:
    state = RuntimeThreadRunEventState()
    event = OutputDelta(
        timestamp="2026-01-01T00:00:00Z",
        content="hi",
        delta_type="assistant_stream",
    )
    result = await normalize_runtime_progress_event(event, state)
    assert result == [event]
    assert state.assistant_stream_text == "hi"


async def test_normalize_runtime_progress_event_normalizes_raw_events() -> None:
    state = RuntimeThreadRunEventState()
    raw = {"method": "token/usage", "params": {"usage": {"totalTokens": 10}}}
    result = await normalize_runtime_progress_event(raw, state)
    assert len(result) == 1
    assert isinstance(result[0], TokenUsage)
    assert result[0].usage == {"totalTokens": 10}


class TestRuntimeTraceFields:
    def test_returns_method_and_progress_at(self) -> None:
        state = RuntimeThreadRunEventState(
            last_runtime_method="session/update",
            last_progress_at="2026-01-01T00:00:00Z",
        )
        fields = runtime_trace_fields(state)
        assert fields == {
            "last_runtime_method": "session/update",
            "last_progress_at": "2026-01-01T00:00:00Z",
        }

    def test_returns_none_when_unset(self) -> None:
        state = RuntimeThreadRunEventState()
        fields = runtime_trace_fields(state)
        assert fields["last_runtime_method"] is None
        assert fields["last_progress_at"] is None


class TestCompletionSourceFromOutcome:
    def test_returns_post_completion_recovery(self) -> None:
        outcome = RuntimeThreadOutcome(
            status="ok",
            assistant_text="done",
            error=None,
            backend_thread_id="t1",
            backend_turn_id="turn-1",
            completion_source="prompt_return",
        )
        assert (
            completion_source_from_outcome(outcome, recovered_after_completion=True)
            == "post_completion_recovery"
        )

    def test_returns_interrupt_for_interrupted_status(self) -> None:
        outcome = RuntimeThreadOutcome(
            status="interrupted",
            assistant_text="",
            error="Runtime thread interrupted",
            backend_thread_id="t1",
            backend_turn_id="turn-1",
        )
        assert (
            completion_source_from_outcome(outcome, recovered_after_completion=False)
            == "interrupt"
        )

    def test_returns_timeout_for_timeout_error(self) -> None:
        outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error="Runtime thread timed out",
            backend_thread_id="t1",
            backend_turn_id="turn-1",
        )
        assert (
            completion_source_from_outcome(outcome, recovered_after_completion=False)
            == "timeout"
        )

    def test_returns_completion_source_when_present(self) -> None:
        outcome = RuntimeThreadOutcome(
            status="ok",
            assistant_text="done",
            error=None,
            backend_thread_id="t1",
            backend_turn_id="turn-1",
            completion_source="idle_completion",
        )
        assert (
            completion_source_from_outcome(outcome, recovered_after_completion=False)
            == "idle_completion"
        )

    def test_returns_prompt_return_as_fallback(self) -> None:
        outcome = RuntimeThreadOutcome(
            status="ok",
            assistant_text="done",
            error=None,
            backend_thread_id="t1",
            backend_turn_id="turn-1",
        )
        assert (
            completion_source_from_outcome(outcome, recovered_after_completion=False)
            == "prompt_return"
        )

    def test_does_not_return_recovery_when_completion_source_is_not_prompt_return(
        self,
    ) -> None:
        outcome = RuntimeThreadOutcome(
            status="ok",
            assistant_text="done",
            error=None,
            backend_thread_id="t1",
            backend_turn_id="turn-1",
            completion_source="idle_completion",
        )
        assert (
            completion_source_from_outcome(outcome, recovered_after_completion=True)
            == "idle_completion"
        )


class TestDecodeFailureObservability:
    """Malformed and unknown payloads must emit explicit RunNotice events instead of silent []."""

    async def test_unknown_method_emits_decode_failure_notice(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "future/unknownMethod", "params": {"data": 42}},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "decode_failure"
        assert events[0].data["reason"] == DECODE_FAILURE_REASON_REGISTRY_MISS
        assert events[0].data["method"] == "future/unknownMethod"
        assert "future/unknownMethod" in events[0].message

    async def test_empty_method_emits_decode_failure_notice(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "", "params": {}},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "decode_failure"
        assert events[0].data["reason"] == DECODE_FAILURE_REASON_EMPTY_METHOD

    async def test_unsupported_shape_dict_without_method_or_message(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"unknown_key": "value", "other": 123},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "decode_failure"
        assert events[0].data["reason"] == DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE

    async def test_dict_with_method_but_non_dict_params(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "session.update", "params": "not a dict"},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "decode_failure"
        assert events[0].data["reason"] == DECODE_FAILURE_REASON_UNSUPPORTED_SHAPE

    async def test_malformed_json_string_emits_empty_messages(self) -> None:
        messages = await decode_runtime_raw_messages("{bad json")

        assert messages == []

    async def test_non_string_non_dict_emits_empty_messages(self) -> None:
        messages = await decode_runtime_raw_messages(42)

        assert messages == []

    async def test_dict_without_method_or_message_emits_empty_messages(self) -> None:
        messages = await decode_runtime_raw_messages({"key": "value"})

        assert messages == []

    async def test_sse_with_malformed_json_data_still_produces_events(self) -> None:
        state = RuntimeThreadRunEventState()

        raw_sse = "event: app-server\ndata: {bad json\n\n"
        events = await normalize_runtime_thread_raw_event(raw_sse, state)

        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "decode_failure"

    async def test_known_method_does_not_emit_decode_failure(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"usage": {"totalTokens": 10}}},
            state,
        )

        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)

    async def test_session_status_idle_does_not_emit_decode_failure(self) -> None:
        state = RuntimeThreadRunEventState()

        events = await normalize_runtime_thread_raw_event(
            {"method": "session.status", "params": {"status": {"type": "idle"}}},
            state,
        )

        assert events == []
        assert state.completed_seen is True

    async def test_empty_string_raw_event_returns_empty(self) -> None:
        messages = await decode_runtime_raw_messages("")

        assert messages == []


class TestRegistryIsSoleDispatchPath:
    """The decoder registry must be the only path for method-based normalization.

    Every supported method must have a registered decoder. Unknown methods
    must produce observable decode_failure notices rather than silent empty lists.
    """

    EXPECTED_REGISTRY_METHODS: list[str] = [
        "item/reasoning/summaryTextDelta",
        "item/completed",
        "item/agentMessage/delta",
        "item/toolCall/start",
        "item/toolCall/end",
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "prompt/output",
        "prompt/delta",
        "prompt/progress",
        "turn/progress",
        "prompt/message",
        "turn/message",
        "prompt/failed",
        "turn/failed",
        "prompt/completed",
        "turn/completed",
        "prompt/cancelled",
        "turn/cancelled",
        "permission/requested",
        "session/request_permission",
        "permission/decision",
        "permission",
        "question",
        "token/usage",
        "usage",
        "turn/tokenUsage",
        "turn/usage",
        "thread/tokenUsage/updated",
        "session/update",
        "session.idle",
        "session.status",
        "session/status",
        "message.part.updated",
        "message.part.delta",
        "message.updated",
        "message.completed",
        "message.delta",
        "turn/streamDelta",
        "turn/error",
        "error",
    ]

    def test_registry_covers_all_expected_methods(self) -> None:
        from codex_autorunner.core.orchestration.runtime_thread_decoders import (
            build_default_decoder_registry,
        )

        registry = build_default_decoder_registry()
        missing = [
            m for m in self.EXPECTED_REGISTRY_METHODS if not registry.has_decoder(m)
        ]
        assert missing == [], f"Registry missing decoders for: {missing}"

    async def test_all_supported_methods_produce_events_or_empty(self) -> None:
        minimal_payloads: dict[str, dict[str, object]] = {
            "item/reasoning/summaryTextDelta": {"delta": "thinking"},
            "item/completed": {"item": {"type": "agentMessage", "text": "hi"}},
            "item/agentMessage/delta": {"delta": "msg"},
            "item/toolCall/start": {"item": {"toolCall": {"name": "t", "input": {}}}},
            "item/toolCall/end": {"name": "t", "result": {}},
            "item/commandExecution/requestApproval": {"command": ["ls"]},
            "item/fileChange/requestApproval": {"files": ["a.py"]},
            "prompt/output": {"delta": "out"},
            "prompt/delta": {"delta": "d"},
            "prompt/progress": {"delta": "prog"},
            "turn/progress": {"delta": "p"},
            "prompt/message": {"text": "msg"},
            "turn/message": {"text": "msg"},
            "prompt/failed": {"message": "err"},
            "turn/failed": {"message": "err"},
            "prompt/completed": {"status": "completed"},
            "turn/completed": {"status": "completed"},
            "prompt/cancelled": {"status": "cancelled"},
            "turn/cancelled": {"status": "cancelled"},
            "permission/requested": {"requestId": "r1"},
            "session/request_permission": {"requestId": "r2"},
            "permission/decision": {"decision": "accept"},
            "permission": {"reason": "ok"},
            "question": {"question": "q"},
            "token/usage": {"usage": {"totalTokens": 1}},
            "usage": {"usage": {"totalTokens": 1}},
            "turn/tokenUsage": {"usage": {"totalTokens": 1}},
            "turn/usage": {"usage": {"totalTokens": 1}},
            "thread/tokenUsage/updated": {"tokenUsage": {"totalTokens": 1}},
            "session/update": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                }
            },
            "session.idle": {},
            "session.status": {"status": {"type": "running"}},
            "session/status": {"status": {"type": "running"}},
            "message.part.updated": {
                "properties": {"part": {"id": "p1", "type": "text", "text": "hi"}}
            },
            "message.part.delta": {
                "properties": {
                    "partID": "p1",
                    "delta": {"text": "hi"},
                }
            },
            "message.updated": {
                "properties": {"info": {"id": "m1", "role": "assistant"}}
            },
            "message.completed": {
                "properties": {"info": {"id": "m1", "role": "assistant"}}
            },
            "message.delta": {"properties": {"delta": {"text": "hi"}}},
            "turn/streamDelta": {"delta": "stream"},
            "turn/error": {"message": "err"},
            "error": {"error": {"message": "err"}},
        }

        for method in self.EXPECTED_REGISTRY_METHODS:
            state = RuntimeThreadRunEventState()
            params = minimal_payloads.get(method, {})
            events = await normalize_runtime_thread_raw_event(
                {"method": method, "params": params},
                state,
            )
            assert not any(
                isinstance(e, RunNotice) and e.kind == "decode_failure" for e in events
            ), f"Supported method {method!r} produced a decode_failure notice"

    async def test_every_unregistered_method_produces_decode_failure(self) -> None:
        unknown_methods = [
            "future/unknownMethod",
            "custom/event",
            "agent/heartbeat",
        ]
        for method in unknown_methods:
            state = RuntimeThreadRunEventState()
            events = await normalize_runtime_thread_raw_event(
                {"method": method, "params": {}},
                state,
            )
            assert len(events) >= 1, f"Unknown method {method!r} returned no events"
            assert any(
                isinstance(e, RunNotice) and e.kind == "decode_failure" for e in events
            ), f"Unknown method {method!r} did not produce a decode_failure notice"

    async def test_supported_method_does_not_emit_registry_miss(self) -> None:
        state = RuntimeThreadRunEventState()
        events = await normalize_runtime_thread_raw_event(
            {"method": "token/usage", "params": {"usage": {"totalTokens": 10}}},
            state,
        )
        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert not any(
            isinstance(e, RunNotice) and e.kind == "decode_failure" for e in events
        )

    def test_no_legacy_re_exports_of_decoder_internals(self) -> None:
        import codex_autorunner.core.orchestration.runtime_thread_events as mod

        removed = [
            "_extract_output_delta",
            "_output_delta_type_for_method",
            "_normalize_tool_name",
            "_extract_agent_message_text",
            "_coerce_dict",
            "_reasoning_buffer_key",
            "_is_commentary_agent_message",
        ]
        for name in removed:
            assert (
                name not in mod.__all__
            ), f"Legacy re-export {name!r} still in __all__"
