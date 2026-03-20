from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .progress_primitives import format_elapsed


@dataclass(frozen=True)
class TurnFooterSummary:
    status: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    elapsed_label: Optional[str] = None
    step: Optional[int] = None
    context_remaining_percent: Optional[int] = None


_ELAPSED_LABEL_RE = re.compile(r"^(?:\d+s|\d+m \d+s|\d+h \d+m)$")


def _has_turn_footer_metadata(summary: TurnFooterSummary) -> bool:
    return any(
        (
            summary.agent,
            summary.model,
            summary.elapsed_label,
            summary.step is not None,
            summary.context_remaining_percent is not None,
        )
    )


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


def _looks_like_elapsed_label(value: str) -> bool:
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return False
    return _ELAPSED_LABEL_RE.fullmatch(normalized) is not None


def _parse_turn_footer_summary(summary_text: Optional[str]) -> TurnFooterSummary:
    if not isinstance(summary_text, str):
        return TurnFooterSummary()
    line = summary_text.strip()
    if not line:
        return TurnFooterSummary()

    status: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    elapsed_label: Optional[str] = None
    step: Optional[int] = None
    context_remaining_percent: Optional[int] = None

    parts = [part.strip() for part in line.split("·") if part.strip()]
    for index, part in enumerate(parts):
        if part.startswith("agent "):
            value = part[len("agent ") :].strip()
            if value:
                agent = value
            continue
        if part.startswith("step "):
            raw_step = part[len("step ") :].strip()
            try:
                step = int(raw_step)
            except (TypeError, ValueError):
                step = None
            continue
        if part.startswith("ctx ") and part.endswith("%"):
            raw_percent = part[len("ctx ") : -1].strip()
            try:
                context_remaining_percent = int(raw_percent)
            except (TypeError, ValueError):
                context_remaining_percent = None
            continue
        if elapsed_label is None and _looks_like_elapsed_label(part):
            elapsed_label = part
            continue
        if index == 0 and status is None:
            status = part
            continue
        if model is None:
            model = part

    return TurnFooterSummary(
        status=status,
        agent=agent,
        model=model,
        elapsed_label=elapsed_label,
        step=step,
        context_remaining_percent=context_remaining_percent,
    )


def format_turn_footer(
    *,
    summary_text: Optional[str],
    token_usage: Optional[dict[str, Any]],
    elapsed_seconds: Optional[float],
    agent: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    parsed = _parse_turn_footer_summary(summary_text)
    context_remaining_percent = (
        parsed.context_remaining_percent
        if parsed.context_remaining_percent is not None
        else _extract_context_usage_percent(token_usage)
    )
    elapsed_label = parsed.elapsed_label
    if elapsed_seconds is not None:
        elapsed_label = format_elapsed(elapsed_seconds)

    header_parts: list[str] = []
    if parsed.status:
        header_parts.append(parsed.status)
    resolved_agent = agent or parsed.agent
    if resolved_agent:
        header_parts.append(f"agent {resolved_agent}")
    resolved_model = model or parsed.model
    if resolved_model:
        header_parts.append(resolved_model)
    if elapsed_label:
        header_parts.append(elapsed_label)
    if parsed.step is not None:
        header_parts.append(f"step {parsed.step}")
    if context_remaining_percent is not None:
        header_parts.append(f"ctx {context_remaining_percent}%")

    lines: list[str] = []
    if header_parts and _has_turn_footer_metadata(parsed):
        lines.append(" · ".join(header_parts))
    token_line = _format_tui_token_usage(token_usage)
    if token_line:
        lines.append(token_line)
    if not lines:
        return None
    return "\n".join(lines)


def compose_turn_response_with_footer(
    response_text: Optional[str],
    *,
    summary_text: Optional[str],
    token_usage: Optional[dict[str, Any]],
    elapsed_seconds: Optional[float],
    agent: Optional[str] = None,
    model: Optional[str] = None,
    empty_response_text: str = "(No response text returned.)",
) -> str:
    body = response_text.strip() if isinstance(response_text, str) else ""
    parsed_summary = _parse_turn_footer_summary(summary_text)
    summary_fallback = (
        summary_text.strip()
        if isinstance(summary_text, str)
        and summary_text.strip()
        and not _has_turn_footer_metadata(parsed_summary)
        else ""
    )
    footer = format_turn_footer(
        summary_text=summary_text,
        token_usage=token_usage,
        elapsed_seconds=elapsed_seconds,
        agent=agent,
        model=model,
    )
    if not body:
        body = summary_fallback or (empty_response_text if footer else "")
    if body and footer:
        return f"{body}\n\n{footer}"
    if footer:
        return footer
    return body
