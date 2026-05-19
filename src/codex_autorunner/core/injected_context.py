from __future__ import annotations

import re

INJECTED_CONTEXT_START = "<injected context>"
INJECTED_CONTEXT_END = "</injected context>"

_INJECTED_CONTEXT_BLOCK_RE = re.compile(
    rf"(?is){re.escape(INJECTED_CONTEXT_START)}\s*(.*?)\s*{re.escape(INJECTED_CONTEXT_END)}"
)


def render_legacy_injected_context_transport(text: str) -> str:
    """Render a model-only capsule for runtimes without native context channels."""
    return f"{INJECTED_CONTEXT_START}\n{text}\n{INJECTED_CONTEXT_END}"


def strip_legacy_injected_context_transport_blocks(text: str | None) -> str | None:
    """Clean archived pre-capsule transcripts; not a read-model correctness boundary."""
    if not isinstance(text, str) or not text:
        return text
    lowered = text.lower()
    if INJECTED_CONTEXT_START not in lowered and INJECTED_CONTEXT_END not in lowered:
        return text
    stripped = _INJECTED_CONTEXT_BLOCK_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def split_legacy_injected_context_transport(
    text: str | None,
) -> tuple[str | None, str | None]:
    """Normalize archived transport markers at backend read-model boundaries.

    Removal note: this exists only for transcripts that predate structured
    model-context fields. New turn paths should provide explicit capsule refs
    and model context metadata instead of relying on these markers.
    """

    if not isinstance(text, str) or not text:
        return text, None
    lowered = text.lower()
    if INJECTED_CONTEXT_START not in lowered and INJECTED_CONTEXT_END not in lowered:
        return text, None
    contexts = [
        match.group(1).strip()
        for match in _INJECTED_CONTEXT_BLOCK_RE.finditer(text)
        if match.group(1).strip()
    ]
    stripped = _INJECTED_CONTEXT_BLOCK_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped or text, "\n\n".join(contexts) or None
