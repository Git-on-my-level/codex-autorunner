import re
from typing import Any, List, Tuple

_REDACTIONS: List[Tuple[re.Pattern[str], str]] = [
    # OpenAI-like keys.
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-[REDACTED]"),
    # GitHub personal access tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh_[REDACTED]"),
    # AWS access key ids (best-effort).
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    # JWT-ish blobs.
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[JWT_REDACTED]",
    ),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_jsonable(value: Any) -> tuple[Any, bool]:
    """Redact known secret patterns in JSON-like values."""

    if isinstance(value, str):
        redacted = redact_text(value)
        return redacted, redacted != value
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            redacted_item, item_changed = redact_jsonable(item)
            changed = changed or item_changed
            items.append(redacted_item)
        return items, changed
    if isinstance(value, tuple):
        changed = False
        items = []
        for item in value:
            redacted_item, item_changed = redact_jsonable(item)
            changed = changed or item_changed
            items.append(redacted_item)
        return tuple(items), changed
    if isinstance(value, dict):
        changed = False
        redacted_dict = {}
        for key, item in value.items():
            redacted_item, item_changed = redact_jsonable(item)
            changed = changed or item_changed
            redacted_dict[key] = redacted_item
        return redacted_dict, changed
    return value, False
