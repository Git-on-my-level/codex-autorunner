from __future__ import annotations

from codex_autorunner.core.acp_lifecycle import (
    analyze_acp_lifecycle_message,
    extract_identifier,
    extract_message_text,
    extract_output_delta,
    extract_permission_description,
    extract_usage,
    is_idle_terminal,
    session_status_type,
    session_update_content_summary,
    should_close_turn_buffer,
    should_map_missing_turn_id,
    status_indicates_interrupted,
    status_indicates_successful_completion,
    terminal_status_for_method,
)


def test_extract_identifier_prefers_first_key() -> None:
    payload = {"turnId": "t1", "turn_id": "t2", "promptId": "t3", "prompt_id": "t4"}
    assert (
        extract_identifier(payload, "turnId", "turn_id", "promptId", "prompt_id")
        == "t1"
    )


def test_extract_identifier_falls_back_to_snake_case() -> None:
    payload = {"turn_id": "t2", "promptId": "t3"}
    assert (
        extract_identifier(payload, "turnId", "turn_id", "promptId", "prompt_id")
        == "t2"
    )


def test_extract_identifier_falls_back_to_promptId() -> None:
    payload = {"promptId": "t3"}
    assert (
        extract_identifier(payload, "turnId", "turn_id", "promptId", "prompt_id")
        == "t3"
    )


def test_extract_identifier_falls_back_to_prompt_id() -> None:
    payload = {"prompt_id": "t4"}
    assert (
        extract_identifier(payload, "turnId", "turn_id", "promptId", "prompt_id")
        == "t4"
    )


def test_extract_identifier_returns_none_for_empty() -> None:
    assert extract_identifier({}, "turnId") is None


def test_extract_identifier_normalizes_int_to_string() -> None:
    result = extract_identifier({"turnId": 123}, "turnId")
    assert result == "123"


def test_extract_usage_prefers_usage_key() -> None:
    result = extract_usage({"usage": {"input": 5}, "tokenUsage": {"input": 10}})
    assert result == {"input": 5}


def test_extract_usage_falls_back_to_tokenUsage() -> None:
    result = extract_usage({"tokenUsage": {"input": 10}})
    assert result == {"input": 10}


def test_extract_usage_returns_empty_for_missing() -> None:
    assert extract_usage({}) == {}


def test_extract_message_text_prefers_finalOutput() -> None:
    result = extract_message_text({"finalOutput": "a", "text": "b", "content": "c"})
    assert result == "a"


def test_extract_message_text_reads_final_output_snake_case() -> None:
    result = extract_message_text({"final_output": "snake"})
    assert result == "snake"


def test_extract_message_text_reads_text() -> None:
    assert extract_message_text({"text": "from text"}) == "from text"


def test_extract_message_text_reads_content() -> None:
    assert extract_message_text({"content": [{"type": "text", "text": "hi"}]}) == "hi"


def test_extract_message_text_reads_message_key() -> None:
    assert extract_message_text({"message": "from message"}) == "from message"


def test_extract_message_text_reads_final_message() -> None:
    assert extract_message_text({"final_message": "from final"}) == "from final"


def test_extract_message_text_reads_output_key() -> None:
    assert extract_message_text({"output": "from output"}) == "from output"


def test_extract_message_text_reads_parts() -> None:
    result = extract_message_text(
        {
            "parts": [
                {"type": "text", "text": "part1"},
                {"type": "text", "text": " part2"},
            ]
        }
    )
    assert result == "part1 part2"


def test_extract_output_delta_reads_delta() -> None:
    assert extract_output_delta({"delta": "d"}) == "d"


def test_extract_output_delta_reads_text() -> None:
    assert extract_output_delta({"text": "t"}) == "t"


def test_extract_output_delta_reads_nested_output() -> None:
    assert extract_output_delta({"output": {"text": "nested"}}) == "nested"


def test_session_status_type_string() -> None:
    assert session_status_type({"status": "idle"}) == "idle"


def test_session_status_type_dict_type_key() -> None:
    assert session_status_type({"status": {"type": "busy"}}) == "busy"


def test_session_status_type_dict_status_key() -> None:
    assert session_status_type({"status": {"status": "running"}}) == "running"


def test_session_status_type_dict_state_key() -> None:
    assert session_status_type({"status": {"state": "active"}}) == "active"


def test_session_status_type_properties_nested() -> None:
    assert session_status_type({"properties": {"status": {"type": "idle"}}}) == "idle"


def test_session_status_type_returns_none_for_empty() -> None:
    assert session_status_type({}) is None


def test_terminal_status_for_method_prompt_completed() -> None:
    assert terminal_status_for_method("prompt/completed", {}) == "completed"


def test_terminal_status_for_method_prompt_completed_with_status() -> None:
    assert (
        terminal_status_for_method("prompt/completed", {"status": "succeeded"})
        == "succeeded"
    )


def test_terminal_status_for_method_prompt_failed() -> None:
    assert terminal_status_for_method("prompt/failed", {}) == "failed"


def test_terminal_status_for_method_prompt_cancelled() -> None:
    assert terminal_status_for_method("prompt/cancelled", {}) == "cancelled"


def test_terminal_status_for_method_turn_completed() -> None:
    assert terminal_status_for_method("turn/completed", {}) == "completed"


def test_terminal_status_for_method_turn_failed() -> None:
    assert terminal_status_for_method("turn/failed", {}) == "failed"


def test_terminal_status_for_method_turn_cancelled() -> None:
    assert terminal_status_for_method("turn/cancelled", {}) == "cancelled"


def test_terminal_status_for_method_session_idle() -> None:
    assert terminal_status_for_method("session.idle", {}) == "completed"


def test_terminal_status_for_method_non_terminal() -> None:
    assert terminal_status_for_method("session/update", {}) is None


def test_status_indicates_successful_completion_variants() -> None:
    for status in ("completed", "complete", "done", "success", "succeeded", "idle"):
        assert status_indicates_successful_completion(
            status, assume_true_when_missing=False
        )


def test_status_indicates_successful_completion_returns_default_when_missing() -> None:
    assert status_indicates_successful_completion(None, assume_true_when_missing=True)


def test_status_indicates_interrupted_variants() -> None:
    for status in ("interrupted", "cancelled", "canceled", "aborted"):
        assert status_indicates_interrupted(status)


def test_status_indicates_interrupted_returns_false_for_success() -> None:
    assert not status_indicates_interrupted("completed")


def test_is_idle_terminal_session_dot_idle() -> None:
    assert is_idle_terminal("session.idle", {}) is True


def test_is_idle_terminal_session_status_idle() -> None:
    assert is_idle_terminal("session.status", {"status": {"type": "idle"}}) is True


def test_is_idle_terminal_session_status_busy() -> None:
    assert is_idle_terminal("session.status", {"status": {"type": "busy"}}) is False


def test_is_idle_terminal_non_idle_method() -> None:
    assert is_idle_terminal("turn/completed", {}) is False


def test_should_map_missing_turn_id_for_session_update() -> None:
    assert should_map_missing_turn_id("session/update", {}) is True


def test_should_map_missing_turn_id_for_session_request_permission() -> None:
    assert should_map_missing_turn_id("session/request_permission", {}) is True


def test_should_map_missing_turn_id_for_terminal() -> None:
    assert should_map_missing_turn_id("turn/completed", {}) is True


def test_should_map_missing_turn_id_for_non_terminal_non_session() -> None:
    assert should_map_missing_turn_id("message.updated", {}) is False


def test_should_close_turn_buffer_completed() -> None:
    assert should_close_turn_buffer("turn/completed", {}) is True


def test_should_close_turn_buffer_session_idle() -> None:
    assert should_close_turn_buffer("session.idle", {}) is False


def test_should_close_turn_buffer_non_terminal() -> None:
    assert should_close_turn_buffer("message.updated", {}) is False


def test_extract_permission_description_from_message() -> None:
    assert (
        extract_permission_description({"message": "Need approval"}) == "Need approval"
    )


def test_extract_permission_description_from_description() -> None:
    assert extract_permission_description({"description": "File write"}) == "File write"


def test_extract_permission_description_from_tool_call_command_string() -> None:
    result = extract_permission_description(
        {"toolCall": {"rawInput": {"command": "rm -rf /"}}}
    )
    assert result == "rm -rf /"


def test_extract_permission_description_from_tool_call_command_list() -> None:
    result = extract_permission_description(
        {"toolCall": {"rawInput": {"command": ["git", "commit", "-m", "msg"]}}}
    )
    assert result == "git commit -m msg"


def test_extract_permission_description_from_tool_call_kind() -> None:
    result = extract_permission_description({"toolCall": {"kind": "shell"}})
    assert result == "shell"


def test_session_update_content_summary_with_text() -> None:
    result = session_update_content_summary(
        {"content": [{"type": "text", "text": "hello"}]}
    )
    assert result["text"] == "hello"
    assert result["content_part_types"] == ("text",)


def test_session_update_content_summary_with_message_fallback() -> None:
    result = session_update_content_summary({"message": "fallback text"})
    assert result["text"] == "fallback text"


def test_session_update_content_summary_missing_content() -> None:
    result = session_update_content_summary({})
    assert result["content_kind"] == "missing"
    assert result["text"] == ""


def test_analyze_acp_lifecycle_session_created() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "session/created", "params": {"sessionId": "s1"}}
    )
    assert snap.normalized_kind == "session"
    assert snap.session_id == "s1"


def test_analyze_acp_lifecycle_session_loaded() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "session/loaded", "params": {"sessionId": "s1"}}
    )
    assert snap.normalized_kind == "session"


def test_analyze_acp_lifecycle_prompt_started() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/started", "params": {"sessionId": "s1"}}
    )
    assert snap.normalized_kind == "turn_started"


def test_analyze_acp_lifecycle_turn_started() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/started", "params": {"sessionId": "s1"}}
    )
    assert snap.normalized_kind == "turn_started"


def test_analyze_acp_lifecycle_prompt_message() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/message", "params": {"text": "hello"}}
    )
    assert snap.normalized_kind == "message"
    assert snap.assistant_text == "hello"


def test_analyze_acp_lifecycle_turn_message() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/message", "params": {"text": "hello"}}
    )
    assert snap.normalized_kind == "message"


def test_analyze_acp_lifecycle_prompt_cancelled_is_terminal() -> None:
    snap = analyze_acp_lifecycle_message({"method": "prompt/cancelled", "params": {}})
    assert snap.normalized_kind == "turn_terminal"
    assert snap.terminal_status == "cancelled"
    assert snap.runtime_terminal_status == "interrupted"


def test_analyze_acp_lifecycle_turn_cancelled_is_terminal() -> None:
    snap = analyze_acp_lifecycle_message({"method": "turn/cancelled", "params": {}})
    assert snap.normalized_kind == "turn_terminal"
    assert snap.terminal_status == "cancelled"
    assert snap.runtime_terminal_status == "interrupted"


def test_analyze_acp_lifecycle_prompt_failed_is_error() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/failed", "params": {"message": "Auth error"}}
    )
    assert snap.normalized_kind == "turn_terminal"
    assert snap.terminal_status == "failed"
    assert snap.runtime_terminal_status == "error"
    assert snap.error_message == "Auth error"


def test_analyze_acp_lifecycle_session_update_usage_update() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "usage_update",
                    "usage": {"input": 10},
                }
            },
        }
    )
    assert snap.normalized_kind == "token_usage"
    assert snap.usage == {"input": 10}


def test_analyze_acp_lifecycle_session_update_session_info() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "session_info_update"}},
        }
    )
    assert snap.normalized_kind == "session"


def test_analyze_acp_lifecycle_snake_case_session_id() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "session/created", "params": {"session_id": "s-snake"}}
    )
    assert snap.session_id == "s-snake"


def test_analyze_acp_lifecycle_snake_case_turn_id() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/started", "params": {"turn_id": "t-snake"}}
    )
    assert snap.turn_id == "t-snake"


def test_analyze_acp_lifecycle_prompt_id_alias() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/started", "params": {"promptId": "p1"}}
    )
    assert snap.turn_id == "p1"


def test_analyze_acp_lifecycle_prompt_id_snake_alias() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/started", "params": {"prompt_id": "p2"}}
    )
    assert snap.turn_id == "p2"


def test_analyze_acp_lifecycle_prompt_progress_delta_alias() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/progress", "params": {"delta": "text chunk"}}
    )
    assert snap.normalized_kind == "output_delta"
    assert snap.output_delta == "text chunk"


def test_analyze_acp_lifecycle_prompt_progress_textDelta_alias() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/progress", "params": {"textDelta": "chunk"}}
    )
    assert snap.normalized_kind == "output_delta"
    assert snap.output_delta == "chunk"


def test_analyze_acp_lifecycle_prompt_progress_text_delta_alias() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "prompt/progress", "params": {"text_delta": "chunk"}}
    )
    assert snap.normalized_kind == "output_delta"
    assert snap.output_delta == "chunk"


def test_analyze_acp_lifecycle_permission_request_id_aliases() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "permission/requested",
            "params": {"requestId": "r1"},
        }
    )
    assert snap.permission_request_id == "r1"


def test_analyze_acp_lifecycle_permission_request_id_snake_case() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "permission/requested",
            "params": {"request_id": "r2"},
        }
    )
    assert snap.permission_request_id == "r2"


def test_analyze_acp_lifecycle_permission_request_id_from_top_level_id() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "id": "r3",
            "method": "permission/requested",
            "params": {},
        }
    )
    assert snap.permission_request_id == "r3"


def test_analyze_acp_lifecycle_session_update_agent_message_chunk() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": "streamed text",
                }
            },
        }
    )
    assert snap.normalized_kind == "output_delta"
    assert snap.output_delta == "streamed text"


def test_analyze_acp_lifecycle_session_update_agent_thought_chunk() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": "thinking...",
                }
            },
        }
    )
    assert snap.normalized_kind == "progress"
    assert snap.progress_message == "thinking..."


def test_analyze_acp_lifecycle_token_usage_method() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "token/usage", "params": {"usage": {"input": 5}}}
    )
    assert snap.normalized_kind == "token_usage"
    assert snap.usage == {"input": 5}


def test_analyze_acp_lifecycle_nested_message_unwrapping() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "message": {
                "method": "turn/completed",
                "params": {"status": "completed"},
            }
        }
    )
    assert snap.normalized_kind == "turn_terminal"
    assert snap.terminal_status == "completed"


def test_analyze_acp_lifecycle_interrupted_status_aborted() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/completed", "params": {"status": "aborted"}}
    )
    assert snap.normalized_kind == "turn_terminal"
    assert snap.runtime_terminal_status == "interrupted"


def test_analyze_acp_lifecycle_interrupted_status_canceled_single_l() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/completed", "params": {"status": "canceled"}}
    )
    assert snap.normalized_kind == "turn_terminal"
    assert snap.runtime_terminal_status == "interrupted"


def test_analyze_acp_lifecycle_successful_status_done() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/completed", "params": {"status": "done"}}
    )
    assert snap.normalized_kind == "turn_terminal"
    assert snap.runtime_terminal_status == "ok"


def test_analyze_acp_lifecycle_successful_status_success() -> None:
    snap = analyze_acp_lifecycle_message(
        {"method": "turn/completed", "params": {"status": "success"}}
    )
    assert snap.normalized_kind == "turn_terminal"
    assert snap.runtime_terminal_status == "ok"


def test_analyze_acp_lifecycle_session_update_snake_case_update_kind() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "session_update": "usage_update",
                    "usage": {"input": 3},
                }
            },
        }
    )
    assert snap.normalized_kind == "token_usage"
    assert snap.usage == {"input": 3}
