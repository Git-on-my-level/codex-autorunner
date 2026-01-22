from __future__ import annotations

import re
from typing import Any, Optional, Tuple

import yaml


_FRONTMATTER_START = re.compile(r"^---\s*$")
_FRONTMATTER_END = re.compile(r"^(---|\.\.\.)\s*$")


def split_markdown_frontmatter(text: str) -> Tuple[Optional[str], str]:
    """Split YAML frontmatter from a markdown document.

    Returns (frontmatter_yaml, body). If no frontmatter is present, frontmatter_yaml is None.
    """

    if not text:
        return None, ""
    lines = text.splitlines()
    if not lines:
        return None, ""
    if not _FRONTMATTER_START.match(lines[0]):
        return None, text

    end_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if _FRONTMATTER_END.match(lines[i]):
            end_idx = i
            break
    if end_idx is None:
        # Malformed frontmatter; treat as absent so callers can surface a lint error.
        return None, text

    fm_yaml = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    if body and not body.startswith("\n"):
        body = "\n" + body
    return fm_yaml, body


def parse_yaml_frontmatter(fm_yaml: Optional[str]) -> dict[str, Any]:
    if fm_yaml is None:
        return {}
    try:
        loaded = yaml.safe_load(fm_yaml)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def parse_markdown_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    fm_yaml, body = split_markdown_frontmatter(text)
    data = parse_yaml_frontmatter(fm_yaml)
    return data, body
