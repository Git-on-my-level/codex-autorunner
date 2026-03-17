"""Shared utilities for command handlers."""

from typing import Any, Optional

from ....agents.opencode.constants import OPENCODE_CONTEXT_WINDOW_KEYS
from ....agents.opencode.usage_decoder import (
    extract_usage,
    extract_usage_field,
    flatten_usage,
)


def _build_opencode_token_usage(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage_payload = extract_usage(payload)
    if usage_payload is None:
        usage_payload = flatten_usage(payload)
    if usage_payload is None:
        return None
    total_tokens = usage_payload.get("totalTokens")
    input_tokens = usage_payload.get("inputTokens")
    cached_tokens = usage_payload.get("cachedInputTokens")
    output_tokens = usage_payload.get("outputTokens")
    reasoning_tokens = usage_payload.get("reasoningTokens")
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
    context_window = extract_usage_field(payload, OPENCODE_CONTEXT_WINDOW_KEYS)
    if context_window is None:
        context_window = extract_usage_field(
            usage_payload, OPENCODE_CONTEXT_WINDOW_KEYS
        )
    if context_window is not None and context_window > 0:
        token_usage["modelContextWindow"] = context_window
    return token_usage


__all__ = ["_build_opencode_token_usage"]
