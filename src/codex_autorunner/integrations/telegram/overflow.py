from __future__ import annotations

from typing import Callable, Optional

from .constants import SHELL_OUTPUT_TRUNCATION_SUFFIX, TELEGRAM_MAX_MESSAGE_LENGTH
from .rendering import _format_telegram_html

RenderFn = Callable[[str], str]


def split_markdown_message(
    text: str,
    *,
    max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    render: Optional[RenderFn] = None,
    include_indicator: bool = True,
) -> list[str]:
    if not text:
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    render = render or _format_telegram_html
    if not include_indicator:
        return _split_markdown_message_chunks(
            text,
            max_len=max_len,
            render=render,
            total_chunks=1,
        )
    total_estimate = 1
    chunks: list[str] = []
    for _ in range(5):
        chunks = _split_markdown_message_chunks(
            text,
            max_len=max_len,
            render=render,
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
    max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    render: Optional[RenderFn] = None,
    suffix: str = SHELL_OUTPUT_TRUNCATION_SUFFIX,
) -> str:
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    render = render or _format_telegram_html
    rendered = render(text)
    if len(rendered) <= max_len:
        return rendered
    trimmed = _trim_text(text, max_len=max_len, suffix=suffix, render=render)
    return render(trimmed)


def _split_once(
    text: str,
    *,
    max_len: int,
    open_fence: Optional[str],
    render: RenderFn,
    indicator: str,
) -> tuple[str, int, Optional[str]]:
    reopen = _reopen_fence(open_fence)
    limit = min(len(text), max_len)
    while True:
        content = _slice_to_boundary(text, limit)
        if not content:
            content = text[: max(1, min(len(text), limit))]
        end_state = _scan_fence_state(content, open_fence=open_fence)
        suffix = _close_fence_suffix(content) if end_state is not None else ""
        raw_chunk = f"{reopen}{content}{suffix}{indicator}"
        rendered = render(raw_chunk)
        if len(rendered) <= max_len or limit <= 1:
            return rendered, len(content), end_state
        overflow = len(rendered) - max_len
        next_limit = limit - overflow - 1
        if next_limit >= limit:
            next_limit = limit - 1
        limit = max(1, next_limit)


def _trim_text(
    text: str,
    *,
    max_len: int,
    suffix: str,
    render: RenderFn,
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
        candidate = f"{content}{suffix}"
        if len(render(candidate)) <= max_len or limit <= 1:
            return candidate
        overflow = len(render(candidate)) - max_len
        next_limit = limit - overflow - 1
        if next_limit >= limit:
            next_limit = limit - 1
        limit = max(1, next_limit)


def _slice_to_boundary(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    para_cut = text.rfind("\n\n", 0, limit + 1)
    if para_cut != -1:
        if para_cut + 2 <= limit:
            return text[: para_cut + 2]
    cut = text.rfind("\n", 0, limit + 1)
    if cut == -1:
        cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = limit
    return text[:cut]


def _scan_fence_state(text: str, *, open_fence: Optional[str]) -> Optional[str]:
    state = open_fence
    for line in text.splitlines():
        fence_info = _parse_fence_line(line)
        if fence_info is None:
            continue
        if state is None:
            state = fence_info
        else:
            state = None
    return state


def _parse_fence_line(line: str) -> Optional[str]:
    stripped = line.lstrip()
    if not stripped.startswith("```"):
        return None
    return stripped[3:].strip()


def _close_fence_suffix(chunk: str) -> str:
    if chunk.endswith("\n"):
        return "```"
    return "\n```"


def _reopen_fence(info: Optional[str]) -> str:
    if info is None:
        return ""
    return f"```{info}\n"


def _split_markdown_message_chunks(
    text: str,
    *,
    max_len: int,
    render: RenderFn,
    total_chunks: int,
) -> list[str]:
    remaining = text
    open_fence: Optional[str] = None
    chunks: list[str] = []
    chunk_index = 1
    while remaining:
        indicator = (
            _continued_indicator(chunk_index, total_chunks) if total_chunks > 1 else ""
        )
        rendered, consumed, open_fence = _split_once(
            remaining,
            max_len=max_len,
            open_fence=open_fence,
            render=render,
            indicator=indicator,
        )
        chunks.append(rendered)
        remaining = remaining[consumed:]
        chunk_index += 1
    return chunks


def _continued_indicator(index: int, total: int) -> str:
    return f"\n\ncontinued ({index}/{total})"
