from __future__ import annotations

from pathlib import Path

from ..tickets.frontmatter import (
    deterministic_ticket_id,
    parse_markdown_frontmatter,
    sanitize_ticket_id,
)


def ticket_stable_id(path: Path) -> str | None:
    """Return the ticket's stable identity token."""
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data, _ = parse_markdown_frontmatter(content)
    explicit_ticket_id = sanitize_ticket_id(data.get("ticket_id"))
    if explicit_ticket_id:
        return explicit_ticket_id
    return deterministic_ticket_id(path)


def ticket_instance_token(path: Path) -> str:
    """Return a stable ticket identity token.

    Uses explicit frontmatter `ticket_id` when present, otherwise a deterministic
    fallback derived from the ticket path.
    """
    ticket_id = ticket_stable_id(path)
    if ticket_id:
        return ticket_id
    if not path.exists():
        return "missing-ticket"
    return "invalid-ticket-id"


def ticket_chat_scope(index: int, path: Path) -> str:
    return f"ticket:{index}:{ticket_instance_token(path)}"


def ticket_state_key(index: int, path: Path) -> str:
    return f"ticket_{index:03d}_{ticket_instance_token(path)}"
