"""Tests for the shared OpenCode part handler factory in utils.py."""

from codex_autorunner.agents.opencode.usage_decoder import (
    extract_usage,
    extract_usage_field,
    flatten_usage,
)
from codex_autorunner.integrations.telegram.handlers.utils import (
    _build_opencode_token_usage,
)


class TestFlattenUsage:
    def test_flattens_simple_tokens(self):
        tokens = {"total": 1000, "input": 500, "output": 500}
        result = flatten_usage(tokens)
        assert result == {"totalTokens": 1000, "inputTokens": 500, "outputTokens": 500}

    def test_flattens_with_reasoning(self):
        tokens = {"total": 1500, "input": 500, "output": 500, "reasoning": 500}
        result = flatten_usage(tokens)
        assert result == {
            "totalTokens": 1500,
            "inputTokens": 500,
            "outputTokens": 500,
            "reasoningTokens": 500,
        }

    def test_flattens_with_cache(self):
        tokens = {"input": 500, "output": 500, "cache": {"read": 200, "write": 100}}
        result = flatten_usage(tokens)
        assert result == {
            "totalTokens": 1300,
            "inputTokens": 500,
            "outputTokens": 500,
            "cachedInputTokens": 200,
            "cacheWriteTokens": 100,
        }

    def test_computes_total_from_components(self):
        tokens = {"input": 500, "output": 300, "reasoning": 200}
        result = flatten_usage(tokens)
        assert result == {
            "totalTokens": 1000,
            "inputTokens": 500,
            "outputTokens": 300,
            "reasoningTokens": 200,
        }

    def test_returns_none_for_empty(self):
        result = flatten_usage({})
        assert result is None


class TestExtractUsage:
    def test_extracts_from_usage_key(self):
        payload = {"usage": {"total": 1000}}
        result = extract_usage(payload)
        assert result == {"totalTokens": 1000}

    def test_extracts_from_tokenUsage_key(self):
        payload = {"tokenUsage": {"total": 1000}}
        result = extract_usage(payload)
        assert result == {"totalTokens": 1000}

    def test_extracts_from_token_usage_key(self):
        payload = {"token_usage": {"total": 1000}}
        result = extract_usage(payload)
        assert result == {"totalTokens": 1000}

    def test_extracts_from_tokens_key(self):
        payload = {"tokens": {"total": 1000, "input": 500}}
        result = extract_usage(payload)
        assert result == {"totalTokens": 1000, "inputTokens": 500}

    def test_returns_none_when_no_usage(self):
        payload = {"other": "data"}
        result = extract_usage(payload)
        assert result is None


class TestExtractUsageField:
    def test_finds_value_in_payload(self):
        payload = {"total": 1000, "totalTokens": 500}
        result = extract_usage_field(payload, ("total", "totalTokens"))
        assert result == 1000

    def test_falls_back_to_second_key(self):
        payload = {"totalTokens": 500}
        result = extract_usage_field(payload, ("total", "totalTokens"))
        assert result == 500

    def test_returns_none_when_not_found(self):
        payload = {}
        result = extract_usage_field(payload, ("total",))
        assert result is None


class TestBuildOpencodeTokenUsage:
    def test_builds_usage_from_total(self):
        payload = {"usage": {"total": 1000}}
        result = _build_opencode_token_usage(payload)
        assert result == {"last": {"totalTokens": 1000}}

    def test_builds_usage_with_components(self):
        payload = {
            "usage": {
                "inputTokens": 500,
                "outputTokens": 300,
                "reasoningTokens": 200,
            }
        }
        result = _build_opencode_token_usage(payload)
        assert result == {
            "last": {
                "totalTokens": 1000,
                "inputTokens": 500,
                "outputTokens": 300,
                "reasoningTokens": 200,
            }
        }

    def test_builds_usage_with_context_window(self):
        payload = {
            "usage": {"total": 1000},
            "modelContextWindow": 200000,
        }
        result = _build_opencode_token_usage(payload)
        assert result == {
            "last": {"totalTokens": 1000},
            "modelContextWindow": 200000,
        }

    def test_computes_total_from_components(self):
        payload = {
            "usage": {
                "inputTokens": 500,
                "outputTokens": 300,
                "reasoningTokens": 200,
            }
        }
        result = _build_opencode_token_usage(payload)
        assert result["last"]["totalTokens"] == 1000

    def test_returns_none_when_no_usage(self):
        payload = {}
        result = _build_opencode_token_usage(payload)
        assert result is None

    def test_includes_cached_tokens(self):
        payload = {
            "usage": {
                "inputTokens": 500,
                "outputTokens": 300,
                "cachedInputTokens": 200,
            }
        }
        result = _build_opencode_token_usage(payload)
        assert result["last"]["cachedInputTokens"] == 200
