from __future__ import annotations

import re
from pathlib import Path

from ..tickets.frontmatter import parse_markdown_frontmatter

_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,128}$")


def _sanitize_ticket_id(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if not _TICKET_ID_RE.match(value):
        return None
    return value


def ticket_instance_token(path: Path) -> str:
    """Return a stable ticket identity token.

    Uses explicit frontmatter `ticket_id` so normal saves (which use atomic_write)
    do not churn identity.
    """
    if not path.exists():
        return "missing-ticket"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "missing-ticket"
    data, _ = parse_markdown_frontmatter(content)
    ticket_id = _sanitize_ticket_id(data.get("ticket_id"))
    if ticket_id:
        return ticket_id
    # Legacy fallback for older tickets that do not yet have ticket_id.
    return f"legacy-{path.name.lower()}"


def ticket_chat_scope(index: int, path: Path) -> str:
    return f"ticket:{index}:{ticket_instance_token(path)}"


def ticket_state_key(index: int, path: Path) -> str:
    return f"ticket_{index:03d}_{ticket_instance_token(path)}"
