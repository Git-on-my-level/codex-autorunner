from codex_autorunner.integrations.telegram.helpers import (
    _extract_context_usage_percent,
    _format_tui_token_usage,
)


def test_extract_context_usage_percent_prefers_last_usage_bucket() -> None:
    token_usage = {
        "last": {"totalTokens": 12000},
        "total": {"totalTokens": 40},
        "modelContextWindow": 20000,
    }
    assert _extract_context_usage_percent(token_usage) == 40


def test_extract_context_usage_percent_uses_context_consumed_percent() -> None:
    token_usage = {
        "last": {"totalTokens": 500},
        "modelContextWindow": 2000,
    }
    assert _extract_context_usage_percent(token_usage) == 75


def test_format_tui_token_usage_uses_last_for_ctx_percent() -> None:
    token_usage = {
        "last": {"totalTokens": 80, "inputTokens": 60, "outputTokens": 20},
        "total": {"totalTokens": 1500, "inputTokens": 1000, "outputTokens": 500},
        "modelContextWindow": 100,
    }
    line = _format_tui_token_usage(token_usage)
    assert line == "Token usage: total 80 input 60 output 20 ctx 20%"
