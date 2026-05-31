from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from .car_context import (
    DEFAULT_PMA_CONTEXT_PROFILE,
    DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    CarContextProfile,
    build_car_context_bundle,
    build_car_context_capsule,
    render_car_context_transport,
)
from .orchestration.turn_context import render_context_capsule_for_prompt
from .surface_context_capsules import (
    append_capsules_to_prompt,
    build_prompt_writing_capsule,
)

CAR_AWARENESS_BLOCK = render_car_context_transport(
    build_car_context_bundle(DEFAULT_PMA_CONTEXT_PROFILE)
)

ROLE_ADDENDUM_START = "<role addendum>"
ROLE_ADDENDUM_END = "</role addendum>"
PROMPT_WRITING_HINT = (
    "If the user asks to write a prompt, put the prompt in a ```code block```."
)
WORKTREE_PR_HINT = (
    "For PMA-managed implementation work that should produce a PR, spawn a "
    "PR-mode managed thread: "
    "`car pma thread spawn --agent <agent_id> --repo <repo_id> --pr --name <label> "
    "--path <hub_root>`. This keeps lifecycle/progress visible and provisions a "
    "fresh hub-owned worktree from `origin/<default-branch>` by default. Do not "
    "use raw `git worktree add ... main` for PMA-managed PR work. If a standalone "
    "hub worktree is explicitly required outside managed-thread creation, use "
    "`car hub worktree create <base_repo_id> <branch> --path <hub_root>`."
)
_PROMPT_CONTEXT_RE = re.compile(r"\bprompt\b", re.IGNORECASE)
_WORKTREE_PR_CONTEXT_RE = re.compile(
    r"\b(?:worktree|worktrees|branch|branches|pr|prs|pull\s+request|pull\s+requests)\b",
    re.IGNORECASE,
)
_FILE_CONTEXT_SIGNAL_RE = re.compile(
    r"(?:<file\s+path=|Inbound Discord attachments:|PMA File Inbox:)",
    re.IGNORECASE,
)
_FILEBOX_HINT_KEYWORD_RE = re.compile(
    r"\b(?:filebox|inbox|outbox)\b",
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
    capsule = build_car_context_capsule(bundle)
    injection = render_context_capsule_for_prompt(capsule) if capsule else ""
    if not injection:
        return prompt_text, False
    if injection in prompt_text:
        return prompt_text, False
    if not prompt_text or not prompt_text.strip():
        return injection, True
    return f"{injection}\n\n{prompt_text}", True


def maybe_inject_prompt_writing_hint(
    prompt_text: str,
    *,
    trigger_text: str | None = None,
) -> tuple[str, bool]:
    """Inject prompt-writing formatting hint when the message is about prompts."""
    if not prompt_text or not prompt_text.strip():
        return prompt_text, False
    if PROMPT_WRITING_HINT in prompt_text:
        return prompt_text, False
    trigger_text = trigger_text if isinstance(trigger_text, str) else prompt_text
    if not _PROMPT_CONTEXT_RE.search(trigger_text):
        return prompt_text, False
    return append_capsules_to_prompt(
        prompt_text, (build_prompt_writing_capsule(PROMPT_WRITING_HINT),)
    )


def has_user_worktree_pr_hint_request(
    user_input_texts: Sequence[str | None] | None,
) -> bool:
    """Return True when raw user text mentions PR/worktree/branch coordination."""
    if not user_input_texts:
        return False
    for text in user_input_texts:
        if not isinstance(text, str):
            continue
        if _WORKTREE_PR_CONTEXT_RE.search(text):
            return True
    return False


def maybe_inject_worktree_pr_hint(
    prompt_text: str,
    *,
    hint_text: str = WORKTREE_PR_HINT,
    user_input_texts: Sequence[str | None] | None = None,
) -> tuple[str, bool]:
    """Inject PMA worktree/PR creation guidance only from raw user signals."""
    if not prompt_text or not prompt_text.strip():
        return prompt_text, False
    if "worktree.pr_mode" in prompt_text or "PR-mode managed thread" in prompt_text:
        return prompt_text, False
    if not has_user_worktree_pr_hint_request(user_input_texts):
        return prompt_text, False
    from .surface_context_capsules import build_model_only_text_capsule

    return append_capsules_to_prompt(
        prompt_text,
        (
            build_model_only_text_capsule(
                capsule_id="worktree.pr_mode",
                text=hint_text,
                reason="worktree_pr_keyword_detected",
            ),
        ),
    )


def has_file_context_signal(prompt_text: str) -> bool:
    """Best-effort signal that prompt already carries file/attachment context."""
    if not prompt_text or not prompt_text.strip():
        return False
    return bool(_FILE_CONTEXT_SIGNAL_RE.search(prompt_text))


def has_user_filebox_hint_request(
    user_input_texts: Sequence[str | None] | None,
) -> bool:
    """Return True when raw user-supplied text explicitly mentions FileBox terms."""
    if not user_input_texts:
        return False
    for text in user_input_texts:
        if not isinstance(text, str):
            continue
        if _FILEBOX_HINT_KEYWORD_RE.search(text):
            return True
    return False


def should_inject_filebox_hint(
    prompt_text: str,
    *,
    has_file_context: bool = False,
    user_input_texts: Sequence[str | None] | None = None,
) -> bool:
    """Gate filebox hints to raw user requests or turns with concrete file context."""
    if not prompt_text or not prompt_text.strip():
        return False
    if (
        "Outbox (pending):" in prompt_text
        or "Inbox:" in prompt_text
        or "Artifact delivery (this turn):" in prompt_text
    ):
        return False
    if not has_file_context and not has_user_filebox_hint_request(user_input_texts):
        return False
    return True


def maybe_inject_filebox_hint(
    prompt_text: str,
    *,
    hint_text: str,
    has_file_context: bool = False,
    user_input_texts: Sequence[str | None] | None = None,
) -> tuple[str, bool]:
    """Inject filebox guidance for explicit user requests or real file context."""
    if not should_inject_filebox_hint(
        prompt_text,
        has_file_context=has_file_context,
        user_input_texts=user_input_texts,
    ):
        return prompt_text, False
    from .surface_context_capsules import build_model_only_text_capsule

    return append_capsules_to_prompt(
        prompt_text,
        (
            build_model_only_text_capsule(
                capsule_id="filebox.uploads",
                text=hint_text,
                reason="file_context_or_filebox_request",
            ),
        ),
    )


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
