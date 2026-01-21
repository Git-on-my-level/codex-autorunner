from codex_autorunner.integrations.telegram.handlers.utils import (
    _build_opencode_token_usage,
)


def test_build_opencode_token_usage_with_total_and_context() -> None:
    usage = _build_opencode_token_usage(
        {"totalTokens": 120, "modelContextWindow": 2000}
    )
    assert usage == {
        "last": {"totalTokens": 120},
        "modelContextWindow": 2000,
    }


def test_build_opencode_token_usage_with_components() -> None:
    usage = _build_opencode_token_usage(
        {
            "usage": {"input_tokens": 50, "output_tokens": 25, "cached_tokens": 5},
            "contextWindow": 1000,
        }
    )
    assert usage == {
        "last": {
            "totalTokens": 80,
            "inputTokens": 50,
            "cachedInputTokens": 5,
            "outputTokens": 25,
        },
        "modelContextWindow": 1000,
    }


def test_build_opencode_token_usage_from_tokens_payload() -> None:
    usage = _build_opencode_token_usage(
        {
            "tokens": {
                "input": 12,
                "output": 7,
                "reasoning": 3,
                "cache": {"read": 2},
            }
        }
    )
    assert usage == {
        "last": {
            "totalTokens": 24,
            "inputTokens": 12,
            "cachedInputTokens": 2,
            "outputTokens": 7,
            "reasoningTokens": 3,
        }
    }
