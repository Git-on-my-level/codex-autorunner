from __future__ import annotations

from typing import Literal

CAR_AWARENESS_BLOCK = """<injected context>
You are operating inside a Codex Autorunner (CAR) managed repo.

CAR’s durable control-plane lives under `.codex-autorunner/`:
- `.codex-autorunner/ABOUT_CAR.md` — short repo-local briefing (ticket/workspace conventions + helper scripts).
- `.codex-autorunner/tickets/` — ordered ticket queue (`TICKET-###*.md`) used by the ticket flow runner.
- `.codex-autorunner/workspace/` — shared context docs:
  - `active_context.md` — current “north star” context; kept fresh for ongoing work.
  - `spec.md` — longer spec / acceptance criteria when needed.
  - `decisions.md` — prior decisions / tradeoffs when relevant.
- `.codex-autorunner/filebox/` — attachments inbox/outbox used by CAR surfaces (if present).

Intent signals: if the user mentions tickets, “dispatch”, “resume”, workspace docs, or `.codex-autorunner/`, they are likely referring to CAR artifacts/workflow rather than generic repo files.

Use the above as orientation. If you need the operational details (exact helper commands, what CAR auto-generates), read `.codex-autorunner/ABOUT_CAR.md`.
</injected context>"""

ROLE_ADDENDUM_START = "<role addendum>"
ROLE_ADDENDUM_END = "</role addendum>"


def format_file_role_addendum(
    kind: Literal["ticket", "workspace", "other"],
    rel_path: str,
) -> str:
    """Format a short role-specific addendum for prompts."""
    if kind == "ticket":
        text = f"This target is a CAR ticket at `{rel_path}`."
    elif kind == "workspace":
        text = f"This target is a CAR workspace doc at `{rel_path}`."
    elif kind == "other":
        text = f"This target file is `{rel_path}`."
    else:
        raise ValueError(f"Unsupported role addendum kind: {kind}")
    return f"{ROLE_ADDENDUM_START}\n{text}\n{ROLE_ADDENDUM_END}"
