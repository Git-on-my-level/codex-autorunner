from __future__ import annotations

import re

INJECTED_CONTEXT_START = "<injected context>"
INJECTED_CONTEXT_END = "</injected context>"

_INJECTED_CONTEXT_BLOCK_RE = re.compile(
    rf"(?is){re.escape(INJECTED_CONTEXT_START)}\s*.*?\s*{re.escape(INJECTED_CONTEXT_END)}"
)


def wrap_injected_context(text: str) -> str:
    """Wrap prompt hints in injected context blocks."""
    return f"{INJECTED_CONTEXT_START}\n{text}\n{INJECTED_CONTEXT_END}"


def strip_injected_context_blocks(text: str | None) -> str | None:
    """Remove injected context blocks from mixed user-visible text."""
    if not isinstance(text, str) or not text:
        return text
    lowered = text.lower()
    if INJECTED_CONTEXT_START not in lowered and INJECTED_CONTEXT_END not in lowered:
        return text
    stripped = _INJECTED_CONTEXT_BLOCK_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()
