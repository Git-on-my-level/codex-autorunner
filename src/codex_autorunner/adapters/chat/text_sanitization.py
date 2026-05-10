from __future__ import annotations

import re

_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*)\n.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNIX_LOCAL_PREFIXES = (
    "/Users/",
    "/workspace/",
    "/home/",
    "/tmp/",
    "/var/",
    "/private/",
    "/etc/",
    "/opt/",
    "/Volumes/",
)


def collapse_local_markdown_links(text: str) -> str:
    """Replace local filesystem markdown links with their display label."""
    if not text:
        return ""
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"\x00CODE{len(placeholders) - 1}\x00"

    sanitized = _CODE_BLOCK_RE.sub(_stash, text)
    sanitized = _INLINE_CODE_RE.sub(_stash, sanitized)

    sanitized = _collapse_local_markdown_links_outside_code(sanitized)
    for index, placeholder in enumerate(placeholders):
        sanitized = sanitized.replace(f"\x00CODE{index}\x00", placeholder)
    return sanitized


def prepare_outbound_source_text(text: str) -> str:
    """Normalize platform-neutral outbound text before surface rendering."""
    if not text:
        return ""
    return collapse_local_markdown_links(text)


def _collapse_local_markdown_links_outside_code(text: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("[", cursor)
        if start < 0:
            parts.append(text[cursor:])
            break
        parts.append(text[cursor:start])
        parsed = _parse_markdown_link(text, start)
        if parsed is None:
            parts.append(text[start])
            cursor = start + 1
            continue
        end, label, target = parsed
        if _is_local_filesystem_target(target):
            parts.append(label)
        else:
            parts.append(text[start:end])
        cursor = end
    return "".join(parts)


def _parse_markdown_link(text: str, start: int) -> tuple[int, str, str] | None:
    if start < 0 or start >= len(text) or text[start] != "[":
        return None
    label_end = text.find("]", start + 1)
    if label_end < 0 or "\n" in text[start + 1 : label_end]:
        return None
    if label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None
    label = text[start + 1 : label_end]
    target_info = _parse_markdown_link_target(text, label_end + 1)
    if target_info is None:
        return None
    target_end, target = target_info
    return target_end, label, target


def _parse_markdown_link_target(text: str, open_paren: int) -> tuple[int, str] | None:
    if open_paren < 0 or open_paren >= len(text) or text[open_paren] != "(":
        return None
    start = open_paren + 1
    if start >= len(text):
        return None
    if text[start] == "<":
        close = text.find(">", start + 1)
        if close < 0 or close + 1 >= len(text) or text[close + 1] != ")":
            return None
        if "\n" in text[start : close + 1]:
            return None
        target = text[start + 1 : close].strip()
        return close + 2, target
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\n":
            return None
        if char == "\\":
            index += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                target = text[start:index].strip()
                return index + 1, target
        index += 1
    return None


def _is_local_filesystem_target(target: str) -> bool:
    if not target:
        return False
    if target.startswith("file://"):
        return True
    if target.startswith("~/"):
        return True
    if target.startswith(_UNIX_LOCAL_PREFIXES):
        return True
    return bool(_WINDOWS_ABSOLUTE_PATH_RE.match(target))
