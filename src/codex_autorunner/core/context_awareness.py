from __future__ import annotations

import re
from typing import Literal

from .car_context import (
    DEFAULT_PMA_CONTEXT_PROFILE,
    DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    CarContextProfile,
    build_car_context_bundle,
    render_injected_car_context,
)
from .injected_context import wrap_injected_context

CAR_AWARENESS_BLOCK = render_injected_car_context(
    build_car_context_bundle(DEFAULT_PMA_CONTEXT_PROFILE)
)

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


def maybe_inject_car_awareness(
    prompt_text: str,
    *,
    declared_profile: CarContextProfile = DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    target_path: str | None = None,
    initiated_by_ticket_flow: bool = False,
) -> tuple[str, bool]:
    """Inject CAR repo awareness context when the selected profile requires it."""
    prompt_text = prompt_text or ""
    bundle = build_car_context_bundle(
        declared_profile,
        prompt_text=prompt_text,
        target_path=target_path,
        initiated_by_ticket_flow=initiated_by_ticket_flow,
    )
    injection = render_injected_car_context(bundle)
    if not injection:
        return prompt_text, False
    if injection in prompt_text:
        return prompt_text, False
    if not prompt_text or not prompt_text.strip():
        return injection, True
    return f"{injection}\n\n{prompt_text}", True


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
