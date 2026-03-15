from __future__ import annotations

import re

_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]*)\n.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((<[^>\n]+>|[^)\n]+)\)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNIX_LOCAL_PREFIXES = (
    "/Users/",
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

    def _replace(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if _is_local_filesystem_target(target):
            return label
        return match.group(0)

    sanitized = _MARKDOWN_LINK_RE.sub(_replace, sanitized)
    for index, placeholder in enumerate(placeholders):
        sanitized = sanitized.replace(f"\x00CODE{index}\x00", placeholder)
    return sanitized


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
