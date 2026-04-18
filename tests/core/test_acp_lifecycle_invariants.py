from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.acp_lifecycle import (
    _IDLE_TERMINAL_METHODS,
    _INTERRUPTED_COMPLETION_STATUSES,
    _SESSION_TURN_ID_FALLBACK_METHODS,
    _SUCCESSFUL_COMPLETION_STATUSES,
    _TERMINAL_METHODS,
    _TEXT_PART_TYPES,
    analyze_acp_lifecycle_message,
    extract_identifier,
    extract_message_text,
    extract_output_delta,
    extract_permission_description,
    extract_usage,
    is_idle_terminal,
    runtime_terminal_status_for_lifecycle,
    session_status_type,
    session_update_content_summary,
    should_close_turn_buffer,
    should_map_missing_turn_id,
    status_indicates_interrupted,
    status_indicates_successful_completion,
    terminal_status_for_method,
)

_CORPUS_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "acp_lifecycle_corpus.json"
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


def test_analyze_acp_lifecycle_session_update_commentary_metadata() -> None:
    snap = analyze_acp_lifecycle_message(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": "draft plan",
                    "phase": "commentary",
                    "alreadyStreamed": True,
                }
            },
        }
    )
    assert snap.normalized_kind == "output_delta"
    assert snap.output_delta == "draft plan"
    assert snap.message_phase == "commentary"
    assert snap.already_streamed is True


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


class TestFrozenMethodSets:
    def test_idle_terminal_methods_are_subset_of_terminal_methods(self) -> None:
        assert _IDLE_TERMINAL_METHODS.issubset(_TERMINAL_METHODS)

    def test_session_fallback_methods_are_not_terminal(self) -> None:
        assert _SESSION_TURN_ID_FALLBACK_METHODS.isdisjoint(_TERMINAL_METHODS)

    def test_idle_methods_do_not_close_buffers(self) -> None:
        for method in _IDLE_TERMINAL_METHODS:
            assert not should_close_turn_buffer(method, {})

    def test_explicit_terminal_methods_close_buffers(self) -> None:
        explicit = _TERMINAL_METHODS - _IDLE_TERMINAL_METHODS
        for method in explicit:
            assert should_close_turn_buffer(method, {}), f"{method} should close buffer"

    def test_idle_terminal_methods_set_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _IDLE_TERMINAL_METHODS.add("fake/method")  # type: ignore[misc]

    def test_terminal_methods_set_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _TERMINAL_METHODS.add("fake/method")  # type: ignore[misc]

    def test_session_fallback_methods_set_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _SESSION_TURN_ID_FALLBACK_METHODS.add("fake/method")  # type: ignore[misc]

    def test_idle_methods_known_set(self) -> None:
        assert _IDLE_TERMINAL_METHODS == frozenset(
            {"session.idle", "session.status", "session/status"}
        )

    def test_terminal_methods_known_set(self) -> None:
        assert _TERMINAL_METHODS == frozenset(
            {
                "prompt/completed",
                "prompt/cancelled",
                "prompt/failed",
                "turn/completed",
                "turn/cancelled",
                "turn/failed",
                "session.idle",
                "session.status",
                "session/status",
            }
        )

    def test_session_fallback_methods_known_set(self) -> None:
        assert _SESSION_TURN_ID_FALLBACK_METHODS == frozenset(
            {"session/update", "session/request_permission"}
        )


class TestIdleTerminalParity:
    def test_session_status_dot_idle_is_idle_terminal(self) -> None:
        assert is_idle_terminal("session.status", {"status": {"type": "idle"}})

    def test_session_status_slash_idle_is_idle_terminal(self) -> None:
        assert is_idle_terminal("session/status", {"status": {"type": "idle"}})

    def test_session_status_dot_string_idle_is_idle_terminal(self) -> None:
        assert is_idle_terminal("session.status", {"status": "idle"})

    def test_session_status_slash_string_idle_is_idle_terminal(self) -> None:
        assert is_idle_terminal("session/status", {"status": "idle"})

    def test_session_status_dot_busy_not_idle_terminal(self) -> None:
        assert not is_idle_terminal("session.status", {"status": {"type": "busy"}})

    def test_session_status_slash_busy_not_idle_terminal(self) -> None:
        assert not is_idle_terminal("session/status", {"status": {"type": "busy"}})

    def test_session_idle_always_idle_terminal(self) -> None:
        assert is_idle_terminal("session.idle", {})

    def test_idle_terminal_does_not_close_buffer(self) -> None:
        assert not should_close_turn_buffer("session.idle", {})

    def test_idle_session_status_dot_does_not_close_buffer(self) -> None:
        assert not should_close_turn_buffer(
            "session.status", {"status": {"type": "idle"}}
        )

    def test_idle_session_status_slash_does_not_close_buffer(self) -> None:
        assert not should_close_turn_buffer(
            "session/status", {"status": {"type": "idle"}}
        )


class TestTurnPromptParity:
    @pytest.mark.parametrize(
        ("prefix"),
        ("prompt", "turn"),
    )
    def test_completed_parity(self, prefix: str) -> None:
        method = f"{prefix}/completed"
        assert terminal_status_for_method(method, {}) == "completed"
        assert runtime_terminal_status_for_lifecycle(method, {}) == "ok"
        assert should_map_missing_turn_id(method, {})
        assert should_close_turn_buffer(method, {})

    @pytest.mark.parametrize(
        ("prefix"),
        ("prompt", "turn"),
    )
    def test_cancelled_parity(self, prefix: str) -> None:
        method = f"{prefix}/cancelled"
        assert terminal_status_for_method(method, {}) == "cancelled"
        assert runtime_terminal_status_for_lifecycle(method, {}) == "interrupted"
        assert should_map_missing_turn_id(method, {})
        assert should_close_turn_buffer(method, {})

    @pytest.mark.parametrize(
        ("prefix"),
        ("prompt", "turn"),
    )
    def test_failed_parity(self, prefix: str) -> None:
        method = f"{prefix}/failed"
        assert terminal_status_for_method(method, {}) == "failed"
        assert runtime_terminal_status_for_lifecycle(method, {}) == "error"
        assert should_map_missing_turn_id(method, {})
        assert should_close_turn_buffer(method, {})

    @pytest.mark.parametrize(
        ("prefix"),
        ("prompt", "turn"),
    )
    def test_started_parity(self, prefix: str) -> None:
        method = f"{prefix}/started"
        assert terminal_status_for_method(method, {}) is None
        assert not should_map_missing_turn_id(method, {})
        assert not should_close_turn_buffer(method, {})

    @pytest.mark.parametrize(
        ("prefix"),
        ("prompt", "turn"),
    )
    def test_message_parity(self, prefix: str) -> None:
        method = f"{prefix}/message"
        snap = analyze_acp_lifecycle_message(
            {"method": method, "params": {"text": "hello"}}
        )
        assert snap.normalized_kind == "message"
        assert snap.assistant_text == "hello"
        assert not snap.is_terminal

    @pytest.mark.parametrize(
        ("prefix"),
        ("prompt", "turn"),
    )
    def test_progress_parity(self, prefix: str) -> None:
        method = f"{prefix}/progress"
        snap = analyze_acp_lifecycle_message(
            {"method": method, "params": {"delta": "chunk"}}
        )
        assert snap.normalized_kind == "output_delta"
        assert snap.output_delta == "chunk"


class TestCorpusSnapshotFields:
    @pytest.fixture(scope="class")
    def corpus(self) -> list[dict[str, object]]:
        raw = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, list)
        return [dict(item) for item in raw]

    def test_corpus_is_not_empty(self, corpus: list[dict[str, object]]) -> None:
        assert len(corpus) >= 10

    @pytest.mark.parametrize(
        ("idx", "case"),
        [
            pytest.param(i, dict(c), id=str(c.get("name", f"case-{i}")))
            for i, c in enumerate(json.loads(_CORPUS_PATH.read_text(encoding="utf-8")))
        ],
    )
    def test_corpus_snapshot_matches_expected(
        self, idx: int, case: dict[str, object]
    ) -> None:
        raw = dict(case["raw"])  # type: ignore[index]
        expected = dict(case["expected"])  # type: ignore[index]
        snap = analyze_acp_lifecycle_message(raw)

        assert snap.normalized_kind == expected["normalized_kind"]
        assert snap.terminal_status == expected["terminal_status"]
        assert snap.runtime_terminal_status == expected["runtime_terminal_status"]
        assert snap.uses_turn_id_fallback == expected["uses_turn_id_fallback"]
        assert snap.closes_turn_buffer == expected["closes_turn_buffer"]
        assert snap.assistant_text == expected["assistant_text"]
        assert snap.output_delta == expected["output_delta"]
        assert snap.progress_message == expected["progress_message"]
        assert snap.error_message == expected["error_message"]
        assert snap.session_status == expected["session_status"]
        assert snap.session_update_kind == expected["session_update_kind"]

        for opt_key in ("usage", "permission_request_id", "permission_description"):
            if opt_key in expected:
                actual = getattr(snap, opt_key)
                assert actual == expected[opt_key]

    def test_corpus_has_turn_completed_variants(
        self, corpus: list[dict[str, object]]
    ) -> None:
        methods = {c["raw"]["method"] for c in corpus}  # type: ignore[index]
        for method in ("turn/completed", "turn/cancelled", "turn/failed"):
            assert method in methods, f"corpus missing {method}"

    def test_corpus_has_session_status_slash_variant(
        self, corpus: list[dict[str, object]]
    ) -> None:
        methods = {c["raw"]["method"] for c in corpus}  # type: ignore[index]
        assert "session/status" in methods

    def test_corpus_has_idle_non_closing_cases(
        self, corpus: list[dict[str, object]]
    ) -> None:
        idle_cases = [
            c
            for c in corpus
            if c.get("expected", {}).get("closes_turn_buffer") is False  # type: ignore[index]
            and c.get("expected", {}).get("terminal_status") is not None  # type: ignore[index]
        ]
        assert len(idle_cases) >= 3

    def test_corpus_has_explicit_terminal_closing_cases(
        self, corpus: list[dict[str, object]]
    ) -> None:
        explicit_cases = [
            c
            for c in corpus
            if c.get("expected", {}).get("closes_turn_buffer") is True  # type: ignore[index]
        ]
        assert len(explicit_cases) >= 5

    def test_corpus_has_session_update_variants(
        self, corpus: list[dict[str, object]]
    ) -> None:
        update_kinds = {
            c.get("expected", {}).get("session_update_kind")  # type: ignore[index]
            for c in corpus
            if c.get("raw", {}).get("method") == "session/update"  # type: ignore[index]
        }
        assert "agent_message_chunk" in update_kinds
        assert "agent_thought_chunk" in update_kinds
        assert "usage_update" in update_kinds
        assert "session_info_update" in update_kinds


class TestTurnIdFallbackBoundary:
    def test_session_update_uses_fallback(self) -> None:
        assert should_map_missing_turn_id("session/update", {})

    def test_session_request_permission_uses_fallback(self) -> None:
        assert should_map_missing_turn_id("session/request_permission", {})

    def test_permission_requested_does_not_use_fallback(self) -> None:
        assert not should_map_missing_turn_id("permission/requested", {})

    def test_prompt_started_does_not_use_fallback(self) -> None:
        assert not should_map_missing_turn_id("prompt/started", {})

    def test_turn_started_does_not_use_fallback(self) -> None:
        assert not should_map_missing_turn_id("turn/started", {})

    def test_token_usage_does_not_use_fallback(self) -> None:
        assert not should_map_missing_turn_id("token/usage", {})

    def test_non_terminal_session_status_does_not_use_fallback(self) -> None:
        assert not should_map_missing_turn_id(
            "session.status", {"status": {"type": "running"}}
        )

    def test_terminal_session_idle_uses_fallback(self) -> None:
        assert should_map_missing_turn_id("session.idle", {})

    def test_terminal_session_status_idle_uses_fallback(self) -> None:
        assert should_map_missing_turn_id(
            "session.status", {"status": {"type": "idle"}}
        )


class TestRuntimeTerminalStatusMapping:
    def test_ok_for_completed(self) -> None:
        assert runtime_terminal_status_for_lifecycle("turn/completed", {}) == "ok"

    def test_ok_for_idle(self) -> None:
        assert runtime_terminal_status_for_lifecycle("session.idle", {}) == "ok"

    def test_ok_for_succeeded(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle(
                "turn/completed", {"status": "succeeded"}
            )
            == "ok"
        )

    def test_ok_for_done(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle("turn/completed", {"status": "done"})
            == "ok"
        )

    def test_ok_for_success(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle(
                "turn/completed", {"status": "success"}
            )
            == "ok"
        )

    def test_interrupted_for_cancelled(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle("turn/cancelled", {}) == "interrupted"
        )

    def test_interrupted_for_aborted(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle(
                "turn/completed", {"status": "aborted"}
            )
            == "interrupted"
        )

    def test_interrupted_for_canceled(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle(
                "turn/completed", {"status": "canceled"}
            )
            == "interrupted"
        )

    def test_error_for_failed(self) -> None:
        assert runtime_terminal_status_for_lifecycle("turn/failed", {}) == "error"

    def test_none_for_non_terminal(self) -> None:
        assert runtime_terminal_status_for_lifecycle("session/update", {}) is None

    def test_none_for_running_session_status(self) -> None:
        assert (
            runtime_terminal_status_for_lifecycle(
                "session.status", {"status": {"type": "running"}}
            )
            is None
        )


class TestSuccessfulCompletionStatusAliases:
    @pytest.mark.parametrize(
        "status", ("completed", "complete", "done", "success", "succeeded", "idle")
    )
    def test_each_alias_recognized_as_successful(self, status: str) -> None:
        assert status_indicates_successful_completion(
            status, assume_true_when_missing=False
        )

    def test_unknown_status_not_successful(self) -> None:
        assert not status_indicates_successful_completion(
            "unknown", assume_true_when_missing=False
        )

    def test_missing_assumes_true_when_flag_set(self) -> None:
        assert status_indicates_successful_completion(
            None, assume_true_when_missing=True
        )

    def test_missing_assumes_false_when_flag_unset(self) -> None:
        assert not status_indicates_successful_completion(
            None, assume_true_when_missing=False
        )


class TestInterruptedStatusAliases:
    @pytest.mark.parametrize(
        "status", ("interrupted", "cancelled", "canceled", "aborted")
    )
    def test_each_alias_recognized_as_interrupted(self, status: str) -> None:
        assert status_indicates_interrupted(status)

    def test_successful_statuses_not_interrupted(self) -> None:
        for status in ("completed", "done", "success"):
            assert not status_indicates_interrupted(status)


class TestSessionStatusSlashDotParity:
    def test_status_dot_and_slash_both_terminal_when_idle(self) -> None:
        assert (
            terminal_status_for_method("session.status", {"status": {"type": "idle"}})
            is not None
        )
        assert (
            terminal_status_for_method("session/status", {"status": {"type": "idle"}})
            is not None
        )

    def test_status_dot_and_slash_both_non_terminal_when_busy(self) -> None:
        assert (
            terminal_status_for_method("session.status", {"status": {"type": "busy"}})
            is None
        )
        assert (
            terminal_status_for_method("session/status", {"status": {"type": "busy"}})
            is None
        )

    def test_both_in_terminal_methods_set(self) -> None:
        assert "session.status" in _TERMINAL_METHODS
        assert "session/status" in _TERMINAL_METHODS

    def test_both_in_idle_methods_set(self) -> None:
        assert "session.status" in _IDLE_TERMINAL_METHODS
        assert "session/status" in _IDLE_TERMINAL_METHODS


class TestUsageIdFieldAliases:
    def test_extract_usage_prefers_usage_over_tokenUsage(self) -> None:
        assert extract_usage({"usage": {"a": 1}, "tokenUsage": {"a": 2}}) == {"a": 1}

    def test_extract_usage_reads_tokenUsage_fallback(self) -> None:
        assert extract_usage({"tokenUsage": {"a": 2}}) == {"a": 2}

    def test_extract_usage_returns_empty_when_both_missing(self) -> None:
        assert extract_usage({}) == {}


class TestSessionIdFieldAliases:
    def test_sessionId_preferred(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {
                "method": "session/created",
                "params": {"sessionId": "camel", "session_id": "snake"},
            }
        )
        assert snap.session_id == "camel"

    def test_session_id_fallback(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "session/created", "params": {"session_id": "snake"}}
        )
        assert snap.session_id == "snake"

    def test_sessionID_fallback(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "session/created", "params": {"sessionID": "upper"}}
        )
        assert snap.session_id == "upper"


class TestTurnIdFieldAliases:
    def test_turnId_preferred(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {
                "method": "turn/started",
                "params": {"turnId": "camel", "turn_id": "snake", "promptId": "p1"},
            }
        )
        assert snap.turn_id == "camel"

    def test_turn_id_fallback(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "turn/started", "params": {"turn_id": "snake", "promptId": "p1"}}
        )
        assert snap.turn_id == "snake"

    def test_promptId_fallback(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "turn/started", "params": {"promptId": "p1", "prompt_id": "p2"}}
        )
        assert snap.turn_id == "p1"

    def test_prompt_id_fallback(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "turn/started", "params": {"prompt_id": "p2"}}
        )
        assert snap.turn_id == "p2"


class TestSessionUpdateKindAliases:
    def test_sessionUpdate_camelCase(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "usage_update"}},
            }
        )
        assert snap.normalized_kind == "token_usage"

    def test_session_update_snake_case(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {
                "method": "session/update",
                "params": {"update": {"session_update": "usage_update"}},
            }
        )
        assert snap.normalized_kind == "token_usage"


class TestOutputDeltaKeyAliases:
    def test_delta_key(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "prompt/progress", "params": {"delta": "d"}}
        )
        assert snap.output_delta == "d"

    def test_textDelta_key(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "prompt/progress", "params": {"textDelta": "td"}}
        )
        assert snap.output_delta == "td"

    def test_text_delta_key(self) -> None:
        snap = analyze_acp_lifecycle_message(
            {"method": "prompt/progress", "params": {"text_delta": "td"}}
        )
        assert snap.output_delta == "td"


class TestTextPartTypeAliases:
    def test_text_type_accepted(self) -> None:
        assert extract_message_text({"parts": [{"type": "text", "text": "a"}]}) == "a"

    def test_output_text_type_accepted(self) -> None:
        assert (
            extract_message_text({"parts": [{"type": "output_text", "text": "a"}]})
            == "a"
        )

    def test_message_type_accepted(self) -> None:
        assert (
            extract_message_text({"parts": [{"type": "message", "text": "a"}]}) == "a"
        )

    def test_agentMessage_type_accepted(self) -> None:
        assert (
            extract_message_text({"parts": [{"type": "agentMessage", "text": "a"}]})
            == "a"
        )

    def test_unknown_type_skipped(self) -> None:
        assert (
            extract_message_text({"parts": [{"type": "tool_call", "text": "skip"}]})
            == ""
        )


class TestSuccessfulCompletionStatusSet:
    def test_known_set(self) -> None:
        assert _SUCCESSFUL_COMPLETION_STATUSES == frozenset(
            {"completed", "complete", "done", "success", "succeeded", "idle"}
        )

    def test_set_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _SUCCESSFUL_COMPLETION_STATUSES.add("speculative")  # type: ignore[misc]

    def test_all_aliases_classified_as_ok(self) -> None:
        for status in _SUCCESSFUL_COMPLETION_STATUSES:
            assert status_indicates_successful_completion(
                status, assume_true_when_missing=False
            ), f"{status} should be successful"

    def test_no_overlap_with_interrupted_set(self) -> None:
        assert _SUCCESSFUL_COMPLETION_STATUSES.isdisjoint(
            _INTERRUPTED_COMPLETION_STATUSES
        )


class TestInterruptedCompletionStatusSet:
    def test_known_set(self) -> None:
        assert _INTERRUPTED_COMPLETION_STATUSES == frozenset(
            {"interrupted", "cancelled", "canceled", "aborted"}
        )

    def test_set_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _INTERRUPTED_COMPLETION_STATUSES.add("speculative")  # type: ignore[misc]

    def test_all_aliases_classified_as_interrupted(self) -> None:
        for status in _INTERRUPTED_COMPLETION_STATUSES:
            assert status_indicates_interrupted(
                status
            ), f"{status} should be interrupted"

    def test_no_overlap_with_successful_set(self) -> None:
        assert _INTERRUPTED_COMPLETION_STATUSES.isdisjoint(
            _SUCCESSFUL_COMPLETION_STATUSES
        )


class TestTextPartTypeSet:
    def test_known_set(self) -> None:
        assert _TEXT_PART_TYPES == frozenset(
            {"text", "output_text", "message", "agentMessage"}
        )

    def test_set_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _TEXT_PART_TYPES.add("speculative")  # type: ignore[misc]

    def test_text_is_primary(self) -> None:
        assert "text" in _TEXT_PART_TYPES

    def test_each_type_extracts_text(self) -> None:
        for part_type in _TEXT_PART_TYPES:
            result = extract_message_text(
                {"parts": [{"type": part_type, "text": "value"}]}
            )
            assert result == "value", f"part type {part_type} should extract text"


class TestTerminalStatusExtractionParity:
    def test_nested_turn_status(self) -> None:
        assert (
            terminal_status_for_method("turn/completed", {"turn": {"status": "done"}})
            == "done"
        )

    def test_nested_prompt_status(self) -> None:
        assert (
            terminal_status_for_method(
                "prompt/completed", {"prompt": {"status": "succeeded"}}
            )
            == "succeeded"
        )

    def test_top_level_status_preferred_over_nested(self) -> None:
        assert (
            terminal_status_for_method(
                "turn/completed",
                {"status": "completed", "turn": {"status": "done"}},
            )
            == "completed"
        )

    def test_prompt_and_turn_completed_both_use_same_aliases(self) -> None:
        for status in (
            _SUCCESSFUL_COMPLETION_STATUSES
            | _INTERRUPTED_COMPLETION_STATUSES
            | {"failed"}
        ):
            for prefix in ("prompt", "turn"):
                method = f"{prefix}/completed"
                if status == "failed":
                    method = f"{prefix}/failed"
                result = terminal_status_for_method(method, {"status": status})
                assert (
                    result is not None
                ), f"{method} with status={status} should be terminal"

    def test_session_idle_has_no_explicit_status(self) -> None:
        assert terminal_status_for_method("session.idle", {}) == "completed"

    def test_session_status_non_idle_not_terminal(self) -> None:
        assert (
            terminal_status_for_method(
                "session.status", {"status": {"type": "running"}}
            )
            is None
        )
