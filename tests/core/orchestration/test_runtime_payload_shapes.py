from __future__ import annotations

from codex_autorunner.core.orchestration.runtime_payload_shapes import (
    OpenCodeToolPartShape,
    TokenUsageShape,
)


class TestTokenUsageShape:
    def test_from_raw_canonical_keys(self) -> None:
        shape = TokenUsageShape.from_raw(
            {
                "totalTokens": 100,
                "inputTokens": 50,
                "outputTokens": 50,
                "cachedInputTokens": 10,
                "reasoningTokens": 5,
                "modelContextWindow": 200000,
            }
        )
        assert shape.total_tokens == 100
        assert shape.input_tokens == 50
        assert shape.output_tokens == 50
        assert shape.cached_tokens == 10
        assert shape.reasoning_tokens == 5
        assert shape.context_window == 200000

    def test_from_raw_snake_case_aliases(self) -> None:
        shape = TokenUsageShape.from_raw(
            {
                "total_tokens": 100,
                "input_tokens": 50,
                "output_tokens": 50,
                "cached_tokens": 10,
                "reasoning_tokens": 5,
                "context_window": 200000,
            }
        )
        assert shape.total_tokens == 100
        assert shape.input_tokens == 50
        assert shape.output_tokens == 50
        assert shape.cached_tokens == 10
        assert shape.reasoning_tokens == 5
        assert shape.context_window == 200000

    def test_from_raw_mixed_aliases(self) -> None:
        shape = TokenUsageShape.from_raw(
            {
                "totalTokens": 100,
                "input_tokens": 50,
                "outputTokens": 50,
                "cached_input_tokens": 10,
            }
        )
        assert shape.total_tokens == 100
        assert shape.input_tokens == 50
        assert shape.output_tokens == 50
        assert shape.cached_tokens == 10

    def test_from_raw_empty_dict(self) -> None:
        shape = TokenUsageShape.from_raw({})
        assert shape.is_empty()
        assert shape.to_dict() == {}

    def test_from_raw_ignores_non_int_values(self) -> None:
        shape = TokenUsageShape.from_raw(
            {
                "totalTokens": "100",
                "inputTokens": None,
                "outputTokens": 50,
            }
        )
        assert shape.total_tokens is None
        assert shape.input_tokens is None
        assert shape.output_tokens == 50

    def test_from_raw_prefers_canonical_key_over_aliases(self) -> None:
        shape = TokenUsageShape.from_raw(
            {
                "totalTokens": 100,
                "total_tokens": 200,
            }
        )
        assert shape.total_tokens == 100

    def test_to_dict_round_trip(self) -> None:
        original = {
            "totalTokens": 100,
            "inputTokens": 50,
            "outputTokens": 50,
            "cachedInputTokens": 10,
            "reasoningTokens": 5,
            "modelContextWindow": 200000,
        }
        shape = TokenUsageShape.from_raw(original)
        assert shape.to_dict() == original

    def test_to_dict_omits_none_fields(self) -> None:
        shape = TokenUsageShape(total_tokens=100, output_tokens=50)
        result = shape.to_dict()
        assert result == {"totalTokens": 100, "outputTokens": 50}
        assert "inputTokens" not in result
        assert "cachedInputTokens" not in result

    def test_is_empty_all_none(self) -> None:
        assert TokenUsageShape().is_empty()

    def test_is_not_empty_when_any_field_set(self) -> None:
        assert not TokenUsageShape(total_tokens=0).is_empty()
        assert not TokenUsageShape(input_tokens=1).is_empty()

    def test_from_raw_partial_data(self) -> None:
        shape = TokenUsageShape.from_raw({"totalTokens": 42})
        assert shape.total_tokens == 42
        assert shape.input_tokens is None
        assert not shape.is_empty()


class TestOpenCodeToolPartShape:
    def test_from_raw_part_minimal(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part({"tool": "bash"})
        assert shape is not None
        assert shape.tool_name == "bash"
        assert shape.tool_id == "bash"
        assert shape.status is None
        assert shape.input_payload == {}
        assert shape.state_payload == {}
        assert shape.error is None

    def test_from_raw_part_with_name_key(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part({"name": "shell"})
        assert shape is not None
        assert shape.tool_name == "shell"

    def test_from_raw_part_with_call_id(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "callID": "call-123"}
        )
        assert shape is not None
        assert shape.tool_id == "call-123"

    def test_from_raw_part_with_id_fallback(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part({"tool": "bash", "id": "part-1"})
        assert shape is not None
        assert shape.tool_id == "part-1"

    def test_from_raw_part_with_state(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {
                "tool": "bash",
                "state": {"status": "running", "exitCode": 0},
            }
        )
        assert shape is not None
        assert shape.status == "running"
        assert shape.state_payload == {"status": "running", "exitCode": 0}
        assert shape.error is None

    def test_from_raw_part_with_error_in_state(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {
                "tool": "bash",
                "state": {"status": "failed", "error": "command not found"},
            }
        )
        assert shape is not None
        assert shape.status == "failed"
        assert shape.error == "command not found"

    def test_from_raw_part_extracts_string_input(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part({"tool": "bash", "input": "pwd"})
        assert shape is not None
        assert shape.input_payload == {"input": "pwd"}

    def test_from_raw_part_extracts_command_key(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "command": "ls -la"}
        )
        assert shape is not None
        assert shape.input_payload == {"command": "ls -la"}

    def test_from_raw_part_extracts_args_dict(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "args": {"command": "echo hi"}}
        )
        assert shape is not None
        assert shape.input_payload == {"command": "echo hi"}

    def test_from_raw_part_extracts_args_string(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part({"tool": "bash", "args": "echo hi"})
        assert shape is not None
        assert shape.input_payload == {"input": "echo hi"}

    def test_from_raw_part_extracts_arguments_key(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "arguments": {"command": "cat file.txt"}}
        )
        assert shape is not None
        assert shape.input_payload == {"command": "cat file.txt"}

    def test_from_raw_part_extracts_params_key(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "params": {"command": "grep pattern"}}
        )
        assert shape is not None
        assert shape.input_payload == {"command": "grep pattern"}

    def test_from_raw_part_prefers_direct_input_over_args(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "input": "pwd", "args": {"command": "ls"}}
        )
        assert shape is not None
        assert shape.input_payload == {"input": "pwd"}

    def test_from_raw_part_returns_none_for_empty_name(self) -> None:
        assert OpenCodeToolPartShape.from_raw_part({"tool": ""}) is None
        assert OpenCodeToolPartShape.from_raw_part({"tool": "  "}) is None
        assert OpenCodeToolPartShape.from_raw_part({}) is None

    def test_from_raw_part_returns_none_for_non_string_name(self) -> None:
        assert OpenCodeToolPartShape.from_raw_part({"tool": 123}) is None
        assert OpenCodeToolPartShape.from_raw_part({"tool": None}) is None

    def test_from_raw_part_strips_whitespace(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part({"tool": "  bash  "})
        assert shape is not None
        assert shape.tool_name == "bash"

    def test_from_raw_part_normalizes_status(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "state": {"status": "Running"}}
        )
        assert shape is not None
        assert shape.status == "running"

    def test_from_raw_part_non_dict_state(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "state": "invalid"}
        )
        assert shape is not None
        assert shape.state_payload == {}
        assert shape.status is None

    def test_from_raw_part_empty_string_input_ignored(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {"tool": "bash", "input": "  ", "args": {"command": "pwd"}}
        )
        assert shape is not None
        assert shape.input_payload == {"command": "pwd"}

    def test_from_raw_part_full_tool_part(self) -> None:
        shape = OpenCodeToolPartShape.from_raw_part(
            {
                "id": "tool-1",
                "callID": "call-abc",
                "tool": "bash",
                "input": "pwd && ls",
                "state": {"status": "completed", "exitCode": 0},
            }
        )
        assert shape is not None
        assert shape.tool_name == "bash"
        assert shape.tool_id == "call-abc"
        assert shape.status == "completed"
        assert shape.input_payload == {"input": "pwd && ls"}
        assert shape.state_payload == {"status": "completed", "exitCode": 0}
