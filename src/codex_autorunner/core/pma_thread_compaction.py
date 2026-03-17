from __future__ import annotations

from typing import Any, Iterable, Optional

DEFAULT_MANAGED_THREAD_COMPACT_TURNS = 6
DEFAULT_MANAGED_THREAD_COMPACT_CHARS = 2000


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _truncate(text: str, limit: int) -> str:
    normalized = _normalize_text(text)
    if limit <= 0 or len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return f"{normalized[: limit - 3].rstrip()}..."


def build_managed_thread_compact_summary(
    turns: Iterable[dict[str, Any]],
    *,
    max_chars: Optional[int] = None,
    max_turns: int = DEFAULT_MANAGED_THREAD_COMPACT_TURNS,
) -> Optional[str]:
    char_budget = (
        max_chars
        if isinstance(max_chars, int) and max_chars > 0
        else DEFAULT_MANAGED_THREAD_COMPACT_CHARS
    )
    if char_budget <= 0:
        return None

    relevant: list[dict[str, Any]] = []
    for turn in turns:
        prompt = _normalize_text(turn.get("prompt"))
        assistant = _normalize_text(turn.get("assistant_text"))
        error = _normalize_text(turn.get("error") or turn.get("error_text"))
        status = _normalize_text(turn.get("status")).lower()
        if not prompt and not assistant and not error:
            continue
        if status not in {"ok", "error", "interrupted"} and not assistant and not error:
            continue
        relevant.append(turn)

    if not relevant:
        return None

    relevant = relevant[-max(1, max_turns) :]
    field_limit = max(96, min(480, char_budget // 3))

    def _render(candidate_turns: list[dict[str, Any]]) -> str:
        lines = ["Compact summary of recent managed thread turns:"]
        for index, turn in enumerate(candidate_turns, start=1):
            prompt = _truncate(str(turn.get("prompt") or ""), field_limit)
            assistant = _truncate(str(turn.get("assistant_text") or ""), field_limit)
            error = _truncate(
                str(turn.get("error") or turn.get("error_text") or ""), field_limit
            )
            status = _normalize_text(turn.get("status")).lower()
            lines.append(f"Turn {index}:")
            if prompt:
                lines.append(f"User: {prompt}")
            if assistant:
                lines.append(f"Assistant: {assistant}")
            elif error:
                label = "Interrupted" if status == "interrupted" else "Error"
                lines.append(f"{label}: {error}")
        return "\n".join(lines).strip()

    selected: list[dict[str, Any]] = []
    for turn in reversed(relevant):
        candidate = [turn, *selected]
        rendered = _render(candidate)
        if len(rendered) > char_budget and selected:
            continue
        if len(rendered) > char_budget:
            selected = [turn]
            break
        selected = candidate

    if not selected:
        return None
    return _truncate(_render(selected), char_budget)


__all__ = [
    "DEFAULT_MANAGED_THREAD_COMPACT_CHARS",
    "DEFAULT_MANAGED_THREAD_COMPACT_TURNS",
    "build_managed_thread_compact_summary",
]
