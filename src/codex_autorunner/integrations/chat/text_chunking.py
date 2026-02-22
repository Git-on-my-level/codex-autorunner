from __future__ import annotations


def chunk_text(
    text: str,
    *,
    max_len: int,
    with_numbering: bool = True,
) -> list[str]:
    if not isinstance(text, str):
        return []
    if not text.strip():
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return [text]

    parts = _split_text(text, max_len)
    if not with_numbering or len(parts) == 1:
        return parts
    return _apply_numbering(text, max_len)


def _apply_numbering(text: str, max_len: int) -> list[str]:
    parts = _split_text(text, max_len)
    total = len(parts)
    while True:
        prefix_len = len(_part_prefix(total, total))
        allowed = max_len - prefix_len
        if allowed <= 0:
            raise ValueError("max_len too small for numbering")
        parts = _split_text(text, allowed)
        new_total = len(parts)
        if new_total == total:
            break
        total = new_total
    return [f"{_part_prefix(idx, total)}{chunk}" for idx, chunk in enumerate(parts, 1)]


def _part_prefix(index: int, total: int) -> str:
    return f"Part {index}/{total}\n"


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunk = remaining[:cut]
        remaining = remaining[cut:]
        parts.append(chunk)
    return parts
