from __future__ import annotations

from ..chat.progress_primitives import (
    ProgressAction,
    TurnProgressTracker,
    render_progress_text,
)

__all__ = [
    "ProgressAction",
    "TurnProgressTracker",
    "render_progress_text",
    "format_elapsed",
]


def format_elapsed(seconds: float) -> str:
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _normalize_output_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _truncate_tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return f"...{text[-(limit - 3) :]}"


def _merge_output_text(current: str, incoming: str) -> str:
    if not current:
        return incoming
    if incoming.startswith(current):
        return incoming
    if current.endswith(incoming):
        return current
    max_overlap = min(len(current), len(incoming))
    for overlap in range(max_overlap, 0, -1):
        if current[-overlap:] == incoming[:overlap]:
            return f"{current}{incoming[overlap:]}"
    return f"{current}{incoming}"


def _output_matches_final_message(output_text: str, final_text: str) -> bool:
    output_norm = _normalize_output_text(output_text).strip()
    final_norm = _normalize_output_text(final_text).strip()
    if not output_norm or not final_norm:
        return False
    if output_norm == final_norm:
        return True
    if output_norm.startswith("..."):
        tail = output_norm[3:]
        if tail and final_norm.endswith(tail):
            return True
    return False
