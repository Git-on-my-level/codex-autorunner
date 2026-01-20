"""Shared utilities for command handlers."""

from typing import Any, Optional


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


_OPENCODE_USAGE_TOTAL_KEYS = ("totalTokens", "total_tokens", "total")
_OPENCODE_USAGE_INPUT_KEYS = (
    "inputTokens",
    "input_tokens",
    "promptTokens",
    "prompt_tokens",
)
_OPENCODE_USAGE_CACHED_KEYS = (
    "cachedInputTokens",
    "cached_input_tokens",
    "cachedTokens",
    "cached_tokens",
)
_OPENCODE_USAGE_OUTPUT_KEYS = (
    "outputTokens",
    "output_tokens",
    "completionTokens",
    "completion_tokens",
)
_OPENCODE_USAGE_REASONING_KEYS = (
    "reasoningTokens",
    "reasoning_tokens",
    "reasoningOutputTokens",
    "reasoning_output_tokens",
)
_OPENCODE_CONTEXT_WINDOW_KEYS = (
    "modelContextWindow",
    "contextWindow",
    "context_window",
    "contextWindowSize",
    "context_window_size",
    "contextLength",
    "context_length",
    "maxTokens",
    "max_tokens",
)
_OPENCODE_MODEL_CONTEXT_KEYS = ("context",) + _OPENCODE_CONTEXT_WINDOW_KEYS


def _flatten_opencode_tokens(tokens: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage: dict[str, Any] = {}
    total_tokens = _coerce_int(tokens.get("total"))
    if total_tokens is not None:
        usage["totalTokens"] = total_tokens
    input_tokens = _coerce_int(tokens.get("input"))
    if input_tokens is not None:
        usage["inputTokens"] = input_tokens
    output_tokens = _coerce_int(tokens.get("output"))
    if output_tokens is not None:
        usage["outputTokens"] = output_tokens
    reasoning_tokens = _coerce_int(tokens.get("reasoning"))
    if reasoning_tokens is not None:
        usage["reasoningTokens"] = reasoning_tokens
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        cached_read = _coerce_int(cache.get("read"))
        if cached_read is not None:
            usage["cachedInputTokens"] = cached_read
        cached_write = _coerce_int(cache.get("write"))
        if cached_write is not None:
            usage["cacheWriteTokens"] = cached_write
    if "totalTokens" not in usage:
        components = [
            usage.get("inputTokens"),
            usage.get("outputTokens"),
            usage.get("reasoningTokens"),
            usage.get("cachedInputTokens"),
            usage.get("cacheWriteTokens"),
        ]
        numeric = [value for value in components if isinstance(value, int)]
        if numeric:
            usage["totalTokens"] = sum(numeric)
    return usage or None


def _extract_opencode_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "usage",
        "tokenUsage",
        "token_usage",
        "usage_stats",
        "usageStats",
        "stats",
    ):
        usage = payload.get(key)
        if isinstance(usage, dict):
            return usage
    tokens = payload.get("tokens")
    if isinstance(tokens, dict):
        flattened = _flatten_opencode_tokens(tokens)
        if flattened:
            return flattened
    return payload


def _extract_opencode_usage_value(
    payload: dict[str, Any], keys: tuple[str, ...]
) -> Optional[int]:
    for key in keys:
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def _build_opencode_token_usage(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage_payload = _extract_opencode_usage_payload(payload)
    total_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_TOTAL_KEYS
    )
    input_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_INPUT_KEYS
    )
    cached_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_CACHED_KEYS
    )
    output_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_OUTPUT_KEYS
    )
    reasoning_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_REASONING_KEYS
    )
    if total_tokens is None:
        components = [
            value
            for value in (
                input_tokens,
                cached_tokens,
                output_tokens,
                reasoning_tokens,
            )
            if isinstance(value, int)
        ]
        if components:
            total_tokens = sum(components)
    if total_tokens is None:
        return None
    usage_line: dict[str, Any] = {"totalTokens": total_tokens}
    if input_tokens is not None:
        usage_line["inputTokens"] = input_tokens
    if cached_tokens is not None:
        usage_line["cachedInputTokens"] = cached_tokens
    if output_tokens is not None:
        usage_line["outputTokens"] = output_tokens
    if reasoning_tokens is not None:
        usage_line["reasoningTokens"] = reasoning_tokens
    token_usage: dict[str, Any] = {"last": usage_line}
    context_window = _extract_opencode_usage_value(
        payload, _OPENCODE_CONTEXT_WINDOW_KEYS
    )
    if context_window is None:
        context_window = _extract_opencode_usage_value(
            usage_payload, _OPENCODE_CONTEXT_WINDOW_KEYS
        )
    if context_window is not None and context_window > 0:
        token_usage["modelContextWindow"] = context_window
    return token_usage
