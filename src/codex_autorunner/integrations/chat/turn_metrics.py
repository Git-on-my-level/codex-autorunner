from __future__ import annotations

from typing import Any, Optional


def _select_context_usage_bucket(
    token_usage: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last")
    if isinstance(last, dict):
        return last
    total = token_usage.get("total")
    if isinstance(total, dict):
        return total
    return None


def _extract_context_usage_percent(
    token_usage: Optional[dict[str, Any]],
) -> Optional[int]:
    usage = _select_context_usage_bucket(token_usage)
    if not isinstance(usage, dict):
        return None
    total_tokens = usage.get("totalTokens")
    if not isinstance(token_usage, dict):
        return None
    context_window = token_usage.get("modelContextWindow")
    if not isinstance(total_tokens, int) or not isinstance(context_window, int):
        return None
    if context_window <= 0:
        return None
    percent_used = round(total_tokens / context_window * 100)
    percent_remaining = max(0, 100 - percent_used)
    return min(percent_remaining, 100)


def _format_tui_token_usage(token_usage: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(token_usage, dict):
        return None
    usage = _select_context_usage_bucket(token_usage)
    if not isinstance(usage, dict):
        return None
    total_tokens = usage.get("totalTokens")
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if not isinstance(total_tokens, int):
        return None
    parts = [f"Token usage: total {total_tokens}"]
    if isinstance(input_tokens, int):
        parts.append(f"input {input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"output {output_tokens}")
    percent = _extract_context_usage_percent(token_usage)
    if percent is not None:
        parts.append(f"ctx {percent}%")
    return " ".join(parts)


def _format_turn_metrics(
    token_usage: Optional[dict[str, Any]],
    elapsed_seconds: Optional[float],
) -> Optional[str]:
    lines: list[str] = []
    if elapsed_seconds is not None:
        lines.append(f"Turn time: {elapsed_seconds:.1f}s")
    token_line = _format_tui_token_usage(token_usage)
    if token_line:
        lines.append(token_line)
    if not lines:
        return None
    return "\n".join(lines)
