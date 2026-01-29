from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..workspace.paths import read_workspace_doc, workspace_doc_path
from .files import list_ticket_paths, safe_relpath


class SpecIngestTicketsError(Exception):
    """Raised when workspace spec → tickets ingest fails."""


@dataclass(frozen=True)
class SpecIngestTicketsResult:
    created: int
    first_ticket_path: Optional[str] = None


def _ticket_dir(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "tickets"


def _ticket_path(repo_root: Path, index: int) -> Path:
    return _ticket_dir(repo_root) / f"TICKET-{index:03d}.md"


def ingest_workspace_spec_to_tickets(repo_root: Path) -> SpecIngestTicketsResult:
    """Generate initial tickets from `.codex-autorunner/workspace/spec.md`.

    Behavior is intentionally conservative:
    - Refuses to run if any tickets already exist.
    - Writes exactly one bootstrap ticket that asks the agent to break down the spec.
    """

    spec_path = workspace_doc_path(repo_root, "spec")
    spec_text = read_workspace_doc(repo_root, "spec")
    if not spec_text.strip():
        raise SpecIngestTicketsError(
            f"Workspace spec is missing or empty at {safe_relpath(spec_path, repo_root)}"
        )

    ticket_dir = _ticket_dir(repo_root)
    existing = list_ticket_paths(ticket_dir)
    if existing:
        raise SpecIngestTicketsError(
            "Tickets already exist; refusing to generate tickets from spec."
        )

    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = _ticket_path(repo_root, 1)

    rel_spec = safe_relpath(spec_path, repo_root)
    template = f"""---
agent: codex
done: false
title: Bootstrap tickets from workspace spec
goal: Read workspace spec and create follow-up tickets
requires:
  - {rel_spec}
---

You are the first ticket in a workspace-driven workflow.

- Read `{rel_spec}`.
- Break the work into additional `TICKET-00X.md` files under `.codex-autorunner/tickets/`.
- Keep this ticket open until the follow-up tickets exist and are coherent.
- Keep tickets small and single-purpose; prefer many small tickets over one big one.

When you need ongoing context, you may also consult (optional):
- `.codex-autorunner/workspace/active_context.md`
- `.codex-autorunner/workspace/decisions.md`
"""

    ticket_path.write_text(template, encoding="utf-8")
    return SpecIngestTicketsResult(
        created=1, first_ticket_path=safe_relpath(ticket_path, repo_root)
    )
