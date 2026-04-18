from __future__ import annotations

from codex_autorunner.core.orchestration.codex_item_normalizers import (
    extract_agent_message_text,
    extract_codex_output_delta,
    extract_codex_usage,
    is_commentary_agent_message,
    merge_runtime_raw_events,
    normalize_tool_name,
    output_delta_type_for_method,
    reasoning_buffer_key,
    runtime_raw_event_key,
)


class TestNormalizeToolName:
    def test_command_execution_item(self) -> None:
        name, inp = normalize_tool_name(
            {"item": {"type": "commandExecution", "command": "ls -la"}}
        )
        assert name == "ls -la"
        assert inp == {"command": "ls -la"}

    def test_command_execution_from_params(self) -> None:
        name, inp = normalize_tool_name(
            {"item": {"type": "commandExecution"}, "command": "pwd"}
        )
        assert name == "pwd"
        assert inp == {"command": "pwd"}

    def test_command_execution_list_command(self) -> None:
        name, inp = normalize_tool_name(
            {"item": {"type": "commandExecution", "command": ["rm", "-rf", "/tmp"]}}
        )
        assert name == "rm -rf /tmp"
        assert inp == {"command": "rm -rf /tmp"}

    def test_command_execution_empty(self) -> None:
        name, inp = normalize_tool_name({"item": {"type": "commandExecution"}})
        assert name == "commandExecution"
        assert inp == {}

    def test_file_change_item(self) -> None:
        name, inp = normalize_tool_name(
            {"item": {"type": "fileChange", "files": ["a.py", "b.py"]}}
        )
        assert name == "fileChange"
        assert inp == {"files": ["a.py", "b.py"]}

    def test_file_change_empty(self) -> None:
        name, inp = normalize_tool_name({"item": {"type": "fileChange"}})
        assert name == "fileChange"
        assert inp == {}

    def test_tool_item(self) -> None:
        name, inp = normalize_tool_name({"item": {"type": "tool", "name": "shell"}})
        assert name == "shell"
        assert inp == {}

    def test_tool_call_nested(self) -> None:
        name, inp = normalize_tool_name(
            {"item": {"toolCall": {"name": "read", "input": {"path": "/etc/hosts"}}}}
        )
        assert name == "read"
        assert inp == {"path": "/etc/hosts"}

    def test_tool_name_from_params(self) -> None:
        name, inp = normalize_tool_name(
            {"toolName": "bash", "toolInput": {"cmd": "ls"}}
        )
        assert name == "bash"
        assert inp == {"cmd": "ls"}

    def test_empty_item(self) -> None:
        name, inp = normalize_tool_name({})
        assert name == ""
        assert inp == {}

    def test_item_kwarg(self) -> None:
        name, inp = normalize_tool_name(
            {},
            item={"type": "commandExecution", "command": "echo hi"},
        )
        assert name == "echo hi"
        assert inp == {"command": "echo hi"}


class TestExtractAgentMessageText:
    def test_text_field(self) -> None:
        assert extract_agent_message_text({"text": "hello"}) == "hello"

    def test_content_list(self) -> None:
        assert (
            extract_agent_message_text(
                {
                    "content": [
                        {"type": "text", "text": "hello "},
                        {"type": "output_text", "text": "world"},
                    ]
                }
            )
            == "hello world"
        )

    def test_skips_non_text_types(self) -> None:
        assert (
            extract_agent_message_text(
                {
                    "content": [
                        {"type": "image", "text": "skip"},
                        {"type": "text", "text": "keep"},
                    ]
                }
            )
            == "keep"
        )

    def test_empty_item(self) -> None:
        assert extract_agent_message_text({}) == ""

    def test_null_type_entries_included(self) -> None:
        assert (
            extract_agent_message_text({"content": [{"text": "no type field"}]})
            == "no type field"
        )


class TestIsCommentaryAgentMessage:
    def test_commentary_phase(self) -> None:
        assert is_commentary_agent_message({"phase": "commentary"}) is True

    def test_commentary_case_insensitive(self) -> None:
        assert is_commentary_agent_message({"phase": "Commentary"}) is True

    def test_non_commentary(self) -> None:
        assert is_commentary_agent_message({"phase": "final"}) is False

    def test_missing_phase(self) -> None:
        assert is_commentary_agent_message({}) is False


class TestOutputDeltaTypeForMethod:
    def test_command_execution_is_log_line(self) -> None:
        assert (
            output_delta_type_for_method("item/commandExecution/outputDelta")
            == "log_line"
        )

    def test_file_change_is_log_line(self) -> None:
        assert output_delta_type_for_method("item/fileChange/outputDelta") == "log_line"

    def test_other_is_stream(self) -> None:
        assert (
            output_delta_type_for_method("item/agentMessage/delta")
            == "assistant_stream"
        )

    def test_case_insensitive(self) -> None:
        assert (
            output_delta_type_for_method("item/commandexecution/outputdelta")
            == "log_line"
        )


class TestExtractCodexOutputDelta:
    def test_delta_key(self) -> None:
        assert extract_codex_output_delta({"delta": "text"}) == "text"

    def test_text_key(self) -> None:
        assert extract_codex_output_delta({"text": "hello"}) == "hello"

    def test_output_key(self) -> None:
        assert extract_codex_output_delta({"output": "result"}) == "result"

    def test_empty(self) -> None:
        assert extract_codex_output_delta({}) == ""

    def test_non_string_skipped(self) -> None:
        assert extract_codex_output_delta({"delta": 42}) == ""


class TestReasoningBufferKey:
    def test_item_id(self) -> None:
        assert reasoning_buffer_key({"itemId": "r1"}) == "r1"

    def test_turn_id(self) -> None:
        assert reasoning_buffer_key({"turnId": "t1"}) == "t1"

    def test_item_kwarg(self) -> None:
        assert reasoning_buffer_key({}, item={"id": "i1"}) == "i1"

    def test_no_key(self) -> None:
        assert reasoning_buffer_key({}) is None

    def test_empty_string_skipped(self) -> None:
        assert reasoning_buffer_key({"itemId": ""}) is None


class TestRuntimeRawEventKey:
    def test_dict(self) -> None:
        key = runtime_raw_event_key({"a": 1, "b": 2})
        assert isinstance(key, str)
        assert "a" in key

    def test_string(self) -> None:
        assert runtime_raw_event_key("hello") == "hello"

    def test_deterministic(self) -> None:
        assert runtime_raw_event_key({"b": 2, "a": 1}) == runtime_raw_event_key(
            {"a": 1, "b": 2}
        )


class TestMergeRuntimeRawEvents:
    def test_no_overlap(self) -> None:
        result = merge_runtime_raw_events(
            [{"a": 1}],
            [{"b": 2}],
        )
        assert len(result) == 2

    def test_full_overlap(self) -> None:
        result = merge_runtime_raw_events(
            [{"a": 1}, {"b": 2}],
            [{"b": 2}, {"c": 3}],
        )
        assert result == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_empty_streamed(self) -> None:
        assert merge_runtime_raw_events([], [{"a": 1}]) == [{"a": 1}]

    def test_empty_result(self) -> None:
        assert merge_runtime_raw_events([{"a": 1}], []) == [{"a": 1}]

    def test_both_empty(self) -> None:
        assert merge_runtime_raw_events([], []) == []


class TestExtractCodexUsage:
    def test_usage_key(self) -> None:
        assert extract_codex_usage({"usage": {"total_tokens": 42}}) == {
            "total_tokens": 42
        }

    def test_token_usage_key(self) -> None:
        assert extract_codex_usage({"tokenUsage": {"input": 10}}) == {"input": 10}

    def test_usage_preferred(self) -> None:
        assert extract_codex_usage({"usage": {"a": 1}, "tokenUsage": {"b": 2}}) == {
            "a": 1
        }

    def test_none_when_missing(self) -> None:
        assert extract_codex_usage({}) is None

    def test_non_dict_returns_none(self) -> None:
        assert extract_codex_usage({"usage": "not a dict"}) is None


class TestCrossBackendConsistency:
    def test_tool_name_agrees_across_consumers(self) -> None:
        params = {
            "item": {
                "type": "commandExecution",
                "command": ["git", "status"],
            }
        }
        shared_name, shared_input = normalize_tool_name(params)
        assert shared_name == "git status"
        assert shared_input == {"command": "git status"}

    def test_agent_message_text_agrees_across_consumers(self) -> None:
        item = {
            "text": "primary text",
            "content": [{"type": "text", "text": "content text"}],
        }
        assert extract_agent_message_text(item) == "primary text"

    def test_agent_message_text_content_list_only(self) -> None:
        item = {
            "content": [
                {"type": "output_text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]
        }
        assert extract_agent_message_text(item) == "hello world"

    def test_output_delta_type_for_method_consistency(self) -> None:
        assert output_delta_type_for_method("turn/streamDelta") == "assistant_stream"
        assert (
            output_delta_type_for_method("item/commandExecution/outputDelta")
            == "log_line"
        )

    def test_reasoning_buffer_key_consistency(self) -> None:
        params = {"itemId": "reason-1", "turnId": "turn-1"}
        assert reasoning_buffer_key(params) == "reason-1"
        assert reasoning_buffer_key({"turnId": "turn-1"}) == "turn-1"

    def test_commentary_filtering_consistency(self) -> None:
        commentary_item = {
            "type": "agentMessage",
            "phase": "commentary",
            "text": "internal",
        }
        assert is_commentary_agent_message(commentary_item) is True
        final_item = {"type": "agentMessage", "phase": "final", "text": "output"}
        assert is_commentary_agent_message(final_item) is False
