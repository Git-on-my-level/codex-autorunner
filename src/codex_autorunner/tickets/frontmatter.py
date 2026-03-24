from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import MutableMapping
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Tuple

import yaml

_FRONTMATTER_START = re.compile(r"^---\s*$")
_FRONTMATTER_END = re.compile(r"^(---|\.\.\.)\s*$")
_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,128}$")


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


def generate_ticket_id() -> str:
    return f"tkt_{uuid.uuid4().hex}"


def deterministic_ticket_id(ticket_path: Path) -> str:
    """Derive a stable ticket_id from the ticket path."""
    identity_path = _ticket_identity_path(ticket_path)
    digest = hashlib.sha256(identity_path.encode("utf-8")).hexdigest()[:32]
    return f"tkt_auto_{digest}"


def _ticket_identity_path(ticket_path: Path) -> str:
    parts = ticket_path.parts
    if not parts:
        return ticket_path.as_posix()
    if not ticket_path.is_absolute():
        return PurePosixPath(*parts).as_posix()
    for idx, part in enumerate(parts):
        if part == ".codex-autorunner":
            return PurePosixPath(*parts[idx:]).as_posix()
    return PurePosixPath(ticket_path.name).as_posix()


def sanitize_ticket_id(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if not _TICKET_ID_RE.match(value):
        return None
    return value


def ensure_ticket_id(
    data: MutableMapping[str, Any],
    *,
    fallback_ticket_id: str | None = None,
) -> str:
    ticket_id = sanitize_ticket_id(data.get("ticket_id"))
    if ticket_id is None:
        ticket_id = sanitize_ticket_id(fallback_ticket_id)
    if ticket_id is None:
        ticket_id = generate_ticket_id()
    data["ticket_id"] = ticket_id
    return ticket_id


def render_markdown_frontmatter(data: dict[str, Any], body: str) -> str:
    normalized = dict(data)
    ensure_ticket_id(normalized)
    fm_yaml = yaml.safe_dump(normalized, sort_keys=False).rstrip()
    normalized_body = body.lstrip("\n").rstrip()
    if normalized_body:
        return f"---\n{fm_yaml}\n---\n\n{normalized_body}\n"
    return f"---\n{fm_yaml}\n---\n"
