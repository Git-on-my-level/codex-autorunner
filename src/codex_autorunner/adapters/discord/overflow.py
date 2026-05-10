from __future__ import annotations

from typing import Optional

from ..chat.markdown_splitting import (
    close_fence_suffix,
    parse_fence_line,
    reopen_fence,
    scan_fence_state,
    slice_to_boundary,
)
from .constants import DISCORD_MAX_MESSAGE_LENGTH

TRUNCATION_SUFFIX = "..."

DEFAULT_MESSAGE_OVERFLOW = "split"
MESSAGE_OVERFLOW_OPTIONS = {"split", "trim", "document"}


def split_markdown_message(
    text: str,
    *,
    max_len: int = DISCORD_MAX_MESSAGE_LENGTH,
    include_indicator: bool = True,
) -> list[str]:
    if not text:
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if not include_indicator:
        return _split_chunks(text, max_len=max_len, total_chunks=1)
    total_estimate = 1
    chunks: list[str] = []
    for _ in range(5):
        chunks = _split_chunks(
            text,
            max_len=max_len,
            total_chunks=total_estimate,
        )
        actual_total = len(chunks)
        if actual_total <= 1:
            return chunks
        if actual_total == total_estimate:
            return chunks
        total_estimate = actual_total
    return chunks


def trim_markdown_message(
    text: str,
    *,
    max_len: int = DISCORD_MAX_MESSAGE_LENGTH,
    suffix: str = TRUNCATION_SUFFIX,
) -> str:
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return text
    trimmed = _trim_text(text, max_len=max_len, suffix=suffix)
    return trimmed


def _split_chunks(
    text: str,
    *,
    max_len: int,
    total_chunks: int,
) -> list[str]:
    remaining = text
    open_fence: Optional[str] = None
    chunks: list[str] = []
    chunk_index = 1
    while remaining:
        prefix = _part_prefix(chunk_index, total_chunks) if total_chunks > 1 else ""
        prefix_len = len(prefix)
        effective_max = max_len - prefix_len
        if effective_max <= 0:
            raise ValueError("max_len too small for numbering prefix")
        chunk, consumed, open_fence = _split_once(
            remaining,
            max_len=effective_max,
            open_fence=open_fence,
        )
        chunks.append(f"{prefix}{chunk}")
        remaining = remaining[consumed:]
        chunk_index += 1
    return chunks


def _split_once(
    text: str,
    *,
    max_len: int,
    open_fence: Optional[str],
) -> tuple[str, int, Optional[str]]:
    reopen = _reopen_fence(open_fence)
    limit = min(len(text), max_len)
    while True:
        content = _slice_to_boundary(text, limit)
        if not content:
            content = text[: max(1, min(len(text), limit))]
        end_state = _scan_fence_state(content, open_fence=open_fence)
        suffix = _close_fence_suffix(content) if end_state is not None else ""
        raw_chunk = f"{reopen}{content}{suffix}"
        if len(raw_chunk) <= max_len or limit <= 1:
            return raw_chunk, len(content), end_state
        overflow = len(raw_chunk) - max_len
        next_limit = limit - overflow - 1
        if next_limit >= limit:
            next_limit = limit - 1
        limit = max(1, next_limit)


def _trim_text(
    text: str,
    *,
    max_len: int,
    suffix: str,
) -> str:
    if not text:
        return text
    if max_len <= len(suffix):
        return suffix[:max_len]
    limit = min(len(text), max_len - len(suffix))
    while True:
        content = _slice_to_boundary(text, limit)
        if not content:
            content = text[: max(1, min(len(text), limit))]
        end_state = _scan_fence_state(content, open_fence=None)
        close_suffix = _close_fence_suffix(content) if end_state is not None else ""
        candidate = f"{content}{close_suffix}{suffix}"
        if len(candidate) <= max_len or limit <= 1:
            return candidate
        overflow = len(candidate) - max_len
        next_limit = limit - overflow - 1
        if next_limit >= limit:
            next_limit = limit - 1
        limit = max(1, next_limit)


_slice_to_boundary = slice_to_boundary
_scan_fence_state = scan_fence_state
_parse_fence_line = parse_fence_line
_close_fence_suffix = close_fence_suffix
_reopen_fence = reopen_fence


def _part_prefix(index: int, total: int) -> str:
    return f"Part {index}/{total}\n"
