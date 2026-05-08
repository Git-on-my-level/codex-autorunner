from __future__ import annotations

from typing import Optional


def slice_to_boundary(text: str, limit: int) -> str:
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


def scan_fence_state(text: str, *, open_fence: Optional[str]) -> Optional[str]:
    state = open_fence
    for line in text.splitlines():
        fence_info = parse_fence_line(line)
        if fence_info is None:
            continue
        if state is None:
            state = fence_info
        else:
            state = None
    return state


def parse_fence_line(line: str) -> Optional[str]:
    stripped = line.lstrip()
    if not stripped.startswith("```"):
        return None
    return stripped[3:].strip()


def close_fence_suffix(chunk: str) -> str:
    if chunk.endswith("\n"):
        return "```"
    return "\n```"


def reopen_fence(info: Optional[str]) -> str:
    if info is None:
        return ""
    return f"```{info}\n"
