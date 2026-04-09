from __future__ import annotations

import json
from typing import Any, Optional

PMA_MAX_REPOS = 25
PMA_MAX_MESSAGES = 10
PMA_MAX_TEXT = 800
PMA_MAX_TEMPLATE_REPOS = 25
PMA_MAX_TEMPLATE_FIELD_CHARS = 120
PMA_MAX_PMA_FILES = 50
PMA_MAX_LIFECYCLE_EVENTS = 20
PMA_MAX_PMA_THREADS = 20
PMA_MAX_AUTOMATION_ITEMS = 10

PMA_DOCS_MAX_CHARS = 12_000
PMA_CONTEXT_LOG_TAIL_LINES = 120


def _tail_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return ""
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def _truncate(text: Optional[str], limit: int) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."


def _trim_extra(extra: Any, limit: int) -> Any:
    if extra is None:
        return None
    if isinstance(extra, str):
        return _truncate(extra, limit)
    try:
        raw = json.dumps(extra, ensure_ascii=True, sort_keys=True, default=str)
    except (ValueError, TypeError):
        raw = str(extra)
    if len(raw) <= limit:
        return extra
    return {
        "_omitted": True,
        "note": "extra omitted due to size",
        "preview": _truncate(raw, limit),
    }
