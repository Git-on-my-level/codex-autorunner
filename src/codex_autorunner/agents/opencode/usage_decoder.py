from __future__ import annotations

from typing import Any, Optional

OPENCODE_USAGE_TOTAL_KEYS = (
    "total",
    "totalTokens",
    "total_tokens",
    "total_tokens_count",
)
OPENCODE_USAGE_INPUT_KEYS = (
    "input",
    "inputTokens",
    "input_tokens",
    "input_tokens_count",
)
OPENCODE_USAGE_OUTPUT_KEYS = (
    "output",
    "outputTokens",
    "output_tokens",
    "output_tokens_count",
)
OPENCODE_USAGE_REASONING_KEYS = ("reasoning", "reasoningTokens", "reasoning_tokens")
OPENCODE_USAGE_CACHED_KEYS = (
    "cachedRead",
    "cached_read",
    "cachedInputTokens",
    "cached_input_tokens",
    "cacheRead",
    "cache_read",
)
OPENCODE_USAGE_CACHE_WRITE_KEYS = (
    "cachedWrite",
    "cached_write",
    "cacheWriteTokens",
    "cache_write_tokens",
)


def coerce_int(value: Any) -> Optional[int]:
    """Coerce a value to int if possible."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return None


def extract_usage(payload: Any) -> Optional[dict[str, Any]]:
    """Extract usage dict from OpenCode payload."""
    if not isinstance(payload, dict):
        return None

    containers = [payload]
    part_containers: list[dict[str, Any]] = []

    def _collect_part_containers(container: Any) -> None:
        if not isinstance(container, dict):
            return
        part = container.get("part")
        if isinstance(part, dict):
            part_containers.append(part)
        parts = container.get("parts")
        if isinstance(parts, list):
            part_containers.extend(entry for entry in parts if isinstance(entry, dict))

    info = payload.get("info")
    if isinstance(info, dict):
        containers.append(info)
        _collect_part_containers(info)

    properties = payload.get("properties")
    if isinstance(properties, dict):
        containers.append(properties)
        _collect_part_containers(properties)
        prop_info = properties.get("info")
        if isinstance(prop_info, dict):
            containers.append(prop_info)
            _collect_part_containers(prop_info)

    response = payload.get("response")
    if isinstance(response, dict):
        containers.append(response)
        _collect_part_containers(response)

    _collect_part_containers(payload)
    containers.extend(part_containers)

    for container in containers:
        for key in (
            "usage",
            "token_usage",
            "tokenUsage",
            "usage_stats",
            "usageStats",
            "stats",
        ):
            usage = container.get(key)
            if isinstance(usage, dict):
                return flatten_usage(usage)

        tokens = container.get("tokens")
        if isinstance(tokens, dict):
            flattened = flatten_usage(tokens)
            if flattened:
                return flattened

    return None


def flatten_usage(tokens: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Flatten OpenCode token usage into standard format."""
    usage: dict[str, Any] = {}

    total_tokens = coerce_int(tokens.get("total"))
    if total_tokens is not None:
        usage["totalTokens"] = total_tokens

    input_tokens = coerce_int(tokens.get("input"))
    if input_tokens is not None:
        usage["inputTokens"] = input_tokens

    output_tokens = coerce_int(tokens.get("output"))
    if output_tokens is not None:
        usage["outputTokens"] = output_tokens

    reasoning_tokens = coerce_int(tokens.get("reasoning"))
    if reasoning_tokens is not None:
        usage["reasoningTokens"] = reasoning_tokens

    cache = tokens.get("cache")
    if isinstance(cache, dict):
        cached_read = coerce_int(cache.get("read"))
        if cached_read is not None:
            usage["cachedInputTokens"] = cached_read
        cached_write = coerce_int(cache.get("write"))
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


def _extract_usage_field(usage: dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    """Extract a usage field by checking multiple key variations."""
    for key in keys:
        if key in usage:
            value = coerce_int(usage.get(key))
            if value is not None:
                return value
    return None
