from __future__ import annotations

import re
from typing import Literal

from .injected_context import wrap_injected_context

CAR_AWARENESS_BLOCK = """<injected context>
You are operating inside a Codex Autorunner (CAR) managed repo.

CAR’s durable control-plane lives under `.codex-autorunner/`:
- `.codex-autorunner/ABOUT_CAR.md` — short repo-local briefing (ticket/contextspace conventions + helper scripts).
- `.codex-autorunner/DESTINATION_QUICKSTART.md` — local/docker runtime setup (custom image + mount/env/profile flags).
- `.codex-autorunner/tickets/` — ordered ticket queue (`TICKET-###*.md`) used by the ticket flow runner.
- `.codex-autorunner/contextspace/` — shared context docs:
  - `active_context.md` — current “north star” context; kept fresh for ongoing work.
  - `spec.md` — longer spec / acceptance criteria when needed.
  - `decisions.md` — prior decisions / tradeoffs when relevant.
- `.codex-autorunner/filebox/` — attachments inbox/outbox used by CAR surfaces (if present).

Intent signals: if the user mentions tickets, “dispatch”, “resume”, contextspace docs, or `.codex-autorunner/`, they are likely referring to CAR artifacts/workflow rather than generic repo files.

Use the above as orientation. If you need operational details (exact helper commands, generated files), read `.codex-autorunner/ABOUT_CAR.md`.
For docker runtime setup details, read `.codex-autorunner/DESTINATION_QUICKSTART.md` or run `car hub destination set --help`.
</injected context>"""

ROLE_ADDENDUM_START = "<role addendum>"
ROLE_ADDENDUM_END = "</role addendum>"
PROMPT_WRITING_HINT = (
    "If the user asks to write a prompt, put the prompt in a ```code block```."
)
_PROMPT_CONTEXT_RE = re.compile(r"\bprompt\b", re.IGNORECASE)
_FILE_CONTEXT_SIGNAL_RE = re.compile(
    r"(?:<file\s+path=|Inbound Discord attachments:|PMA File Inbox:)",
    re.IGNORECASE,
)


def maybe_inject_car_awareness(prompt_text: str) -> tuple[str, bool]:
    """Inject CAR repo awareness context once at the top of the prompt."""
    prompt_text = prompt_text or ""
    if CAR_AWARENESS_BLOCK in prompt_text:
        return prompt_text, False
    if not prompt_text or not prompt_text.strip():
        return CAR_AWARENESS_BLOCK, True
    return f"{CAR_AWARENESS_BLOCK}\n\n{prompt_text}", True


def maybe_inject_prompt_writing_hint(prompt_text: str) -> tuple[str, bool]:
    """Inject prompt-writing formatting hint when the message is about prompts."""
    if not prompt_text or not prompt_text.strip():
        return prompt_text, False
    if PROMPT_WRITING_HINT in prompt_text:
        return prompt_text, False
    if not _PROMPT_CONTEXT_RE.search(prompt_text):
        return prompt_text, False
    return _append_injected_context(
        prompt_text,
        wrap_injected_context(PROMPT_WRITING_HINT),
    )


def has_file_context_signal(prompt_text: str) -> bool:
    """Best-effort signal that prompt already carries file/attachment context."""
    if not prompt_text or not prompt_text.strip():
        return False
    return bool(_FILE_CONTEXT_SIGNAL_RE.search(prompt_text))


def should_inject_filebox_hint(
    prompt_text: str,
    *,
    has_file_context: bool = False,
) -> bool:
    """Gate filebox hints to turns with concrete file context."""
    if not prompt_text or not prompt_text.strip():
        return False
    if "Outbox (pending):" in prompt_text or "Inbox:" in prompt_text:
        return False
    if not has_file_context and not has_file_context_signal(prompt_text):
        return False
    return True


def maybe_inject_filebox_hint(
    prompt_text: str,
    *,
    hint_text: str,
    has_file_context: bool = False,
) -> tuple[str, bool]:
    """Inject filebox guidance only when file context is available."""
    if not should_inject_filebox_hint(
        prompt_text,
        has_file_context=has_file_context,
    ):
        return prompt_text, False
    return _append_injected_context(prompt_text, hint_text)


def _append_injected_context(prompt_text: str, injection: str) -> tuple[str, bool]:
    if prompt_text.strip():
        separator = "\n" if prompt_text.endswith("\n") else "\n\n"
        return f"{prompt_text}{separator}{injection}", True
    return injection, True


def format_file_role_addendum(
    kind: Literal["ticket", "contextspace", "other"],
    rel_path: str,
) -> str:
    """Format a short role-specific addendum for prompts."""
    if kind == "ticket":
        text = f"This target is a CAR ticket at `{rel_path}`."
    elif kind == "contextspace":
        text = f"This target is a CAR contextspace doc at `{rel_path}`."
    elif kind == "other":
        text = f"This target file is `{rel_path}`."
    else:
        raise ValueError(f"Unsupported role addendum kind: {kind}")
    return f"{ROLE_ADDENDUM_START}\n{text}\n{ROLE_ADDENDUM_END}"
