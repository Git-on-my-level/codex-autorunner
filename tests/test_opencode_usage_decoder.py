"""Characterization tests for usage_decoder canonical contract."""

from codex_autorunner.agents.opencode.usage_decoder import (
    coerce_int,
    extract_usage,
    flatten_usage,
)


class TestCoerceInt:
    def test_returns_int_unchanged(self) -> None:
        assert coerce_int(42) == 42
        assert coerce_int(0) == 0
        assert coerce_int(-5) == -5

    def test_converts_string_to_int(self) -> None:
        assert coerce_int("42") == 42
        assert coerce_int("0") == 0

    def test_returns_none_for_invalid_string(self) -> None:
        assert coerce_int("not a number") is None
        assert coerce_int("") is None

    def test_returns_none_for_none(self) -> None:
        assert coerce_int(None) is None

    def test_returns_none_for_float(self) -> None:
        assert coerce_int(3.14) is None
        assert coerce_int(42.0) is None


class TestFlattenUsage:
    def test_flattens_total_input_output(self) -> None:
        tokens = {"total": 100, "input": 60, "output": 40}
        result = flatten_usage(tokens)
        assert result == {
            "totalTokens": 100,
            "inputTokens": 60,
            "outputTokens": 40,
        }

    def test_includes_reasoning_tokens(self) -> None:
        tokens = {"total": 150, "input": 100, "output": 30, "reasoning": 20}
        result = flatten_usage(tokens)
        assert result["reasoningTokens"] == 20

    def test_handles_cache_dict(self) -> None:
        tokens = {"input": 100, "output": 50, "cache": {"read": 30, "write": 10}}
        result = flatten_usage(tokens)
        assert result["cachedInputTokens"] == 30
        assert result["cacheWriteTokens"] == 10

    def test_computes_total_from_components(self) -> None:
        tokens = {"input": 100, "output": 50, "reasoning": 25}
        result = flatten_usage(tokens)
        assert result["totalTokens"] == 175

    def test_returns_none_for_empty_dict(self) -> None:
        assert flatten_usage({}) is None

    def test_handles_partial_cache(self) -> None:
        tokens = {"input": 50, "cache": {"read": 10}}
        result = flatten_usage(tokens)
        assert result["cachedInputTokens"] == 10
        assert "cacheWriteTokens" not in result

    def test_ignores_non_int_values_but_computes_from_components(self) -> None:
        tokens = {"total": "invalid", "input": 50}
        result = flatten_usage(tokens)
        assert result["inputTokens"] == 50
        assert result["totalTokens"] == 50


class TestExtractUsage:
    def test_extracts_usage_from_top_level(self) -> None:
        payload = {"usage": {"total": 100}}
        result = extract_usage(payload)
        assert result == {"totalTokens": 100}

    def test_extracts_from_token_usage_alias(self) -> None:
        payload = {"token_usage": {"input": 50, "output": 30}}
        result = extract_usage(payload)
        assert result["inputTokens"] == 50
        assert result["outputTokens"] == 30

    def test_extracts_from_tokenUsage_camelcase(self) -> None:
        payload = {"tokenUsage": {"input": 50, "output": 30}}
        result = extract_usage(payload)
        assert result["inputTokens"] == 50

    def test_extracts_from_usage_stats_alias(self) -> None:
        payload = {"usage_stats": {"total": 100}}
        result = extract_usage(payload)
        assert result["totalTokens"] == 100

    def test_extracts_from_usageStats_camelcase(self) -> None:
        payload = {"usageStats": {"total": 100}}
        result = extract_usage(payload)
        assert result["totalTokens"] == 100

    def test_extracts_from_stats_alias(self) -> None:
        payload = {"stats": {"input": 20, "output": 10}}
        result = extract_usage(payload)
        assert result["inputTokens"] == 20

    def test_extracts_from_info_nested(self) -> None:
        payload = {"info": {"usage": {"total": 200}}}
        result = extract_usage(payload)
        assert result["totalTokens"] == 200

    def test_extracts_from_properties_info_nested(self) -> None:
        payload = {"properties": {"info": {"tokens": {"input": 100, "output": 50}}}}
        result = extract_usage(payload)
        assert result["inputTokens"] == 100
        assert result["outputTokens"] == 50

    def test_extracts_from_response_nested(self) -> None:
        payload = {"response": {"usage": {"total": 300}}}
        result = extract_usage(payload)
        assert result["totalTokens"] == 300

    def test_extracts_from_tokens_key(self) -> None:
        payload = {"tokens": {"input": 75, "output": 25}}
        result = extract_usage(payload)
        assert result["inputTokens"] == 75
        assert result["outputTokens"] == 25

    def test_extracts_from_part_nested(self) -> None:
        payload = {"part": {"usage": {"total": 150}}}
        result = extract_usage(payload)
        assert result["totalTokens"] == 150

    def test_extracts_from_parts_list(self) -> None:
        payload = {"parts": [{"usage": {"total": 100}}, {"other": "data"}]}
        result = extract_usage(payload)
        assert result["totalTokens"] == 100

    def test_extracts_from_deep_nested_info_part(self) -> None:
        payload = {
            "properties": {
                "info": {"part": {"tokens": {"input": 40, "output": 20}}},
            }
        }
        result = extract_usage(payload)
        assert result["inputTokens"] == 40
        assert result["outputTokens"] == 20

    def test_returns_none_for_non_dict(self) -> None:
        assert extract_usage(None) is None
        assert extract_usage("not a dict") is None
        assert extract_usage(42) is None

    def test_returns_none_when_no_usage_found(self) -> None:
        payload = {"other": "data", "message": "hello"}
        assert extract_usage(payload) is None

    def test_prefers_first_match_in_container_order(self) -> None:
        payload = {"usage": {"total": 100}, "info": {"usage": {"total": 200}}}
        result = extract_usage(payload)
        assert result["totalTokens"] == 100

    def test_handles_complex_nested_structure(self) -> None:
        payload = {
            "sessionID": "s1",
            "properties": {
                "part": {
                    "type": "step-finish",
                    "tokens": {
                        "input": 11,
                        "output": 4,
                        "reasoning": 1,
                        "cache": {"read": 2},
                    },
                },
                "info": {"modelContextWindow": 4096},
            },
        }
        result = extract_usage(payload)
        assert result["inputTokens"] == 11
        assert result["outputTokens"] == 4
        assert result["reasoningTokens"] == 1
        assert result["cachedInputTokens"] == 2
        assert result["totalTokens"] == 18


class TestExtractUsageAliasHeavyPayloads:
    def test_message_part_tokens_legacy_shape(self) -> None:
        payload = {
            "sessionID": "s1",
            "properties": {
                "part": {
                    "type": "step-finish",
                    "tokens": {
                        "input": 10,
                        "output": 5,
                        "reasoning": 2,
                        "cache": {"read": 1},
                    },
                }
            },
        }
        result = extract_usage(payload)
        assert result["totalTokens"] == 18
        assert result["inputTokens"] == 10
        assert result["outputTokens"] == 5
        assert result["reasoningTokens"] == 2
        assert result["cachedInputTokens"] == 1

    def test_message_updated_info_tokens(self) -> None:
        payload = {
            "sessionID": "s1",
            "properties": {
                "info": {"tokens": {"input": 10, "output": 5, "cache": {"read": 2}}}
            },
        }
        result = extract_usage(payload)
        assert result["totalTokens"] == 17
        assert result["cachedInputTokens"] == 2

    def test_real_opencode_stream_shape(self) -> None:
        payload = {
            "type": "message.part.updated",
            "sessionID": "sess-abc123",
            "properties": {
                "part": {
                    "id": "part-xyz",
                    "type": "step-finish",
                    "tokens": {"input": 1523, "output": 287, "cache": {"read": 1200}},
                },
                "delta": {},
            },
        }
        result = extract_usage(payload)
        assert result["inputTokens"] == 1523
        assert result["outputTokens"] == 287
        assert result["cachedInputTokens"] == 1200
        assert result["totalTokens"] == 3010
