from __future__ import annotations


def merge_assistant_stream_text(current: str, incoming: str) -> str:
    """Merge overlapping streamed assistant chunks without duplicating prefixes."""
    if not incoming:
        return current
    if not current:
        return incoming
    if incoming == current:
        return current
    if len(incoming) > len(current) and incoming.startswith(current):
        return incoming
    max_overlap = min(len(current), max(len(incoming) - 1, 0))
    for overlap in range(max_overlap, 0, -1):
        if current[-overlap:] == incoming[:overlap]:
            return f"{current}{incoming[overlap:]}"
    return f"{current}{incoming}"
