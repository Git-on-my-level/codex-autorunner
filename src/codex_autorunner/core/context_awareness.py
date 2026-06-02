from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .car_context import (
    DEFAULT_PMA_CONTEXT_PROFILE,
    DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    CarContextProfile,
    build_car_context_bundle,
    build_car_context_capsule,
    render_car_context_transport,
)
from .context_capsule_planner import (
    plan_context_capsules_for_prompt,
    record_context_capsule_renders,
)
from .context_capsules import (
    ContextCapsule,
    ContextCapsuleExpiry,
    ContextCapsuleRenderPlan,
    ContextCapsuleScope,
    ContextCapsuleVisibility,
    stable_json_digest,
)
from .orchestration.context_capsule_ledger import SQLiteContextCapsuleLedger
from .orchestration.sqlite import open_orchestration_sqlite
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
_PROMPT_CONTEXT_RE = re.compile(
    r"\b(?:write|create|draft|compose|generate)\s+(?:a\s+)?prompt\b",
    re.IGNORECASE,
)
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
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedPromptInjection:
    prompt_text: str
    injected: bool
    render_plans: tuple[ContextCapsuleRenderPlan, ...] = ()


def maybe_inject_car_awareness(
    prompt_text: str,
    *,
    declared_profile: CarContextProfile = DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    target_path: str | None = None,
    initiated_by_ticket_flow: bool = False,
) -> tuple[str, bool]:
    """Inject CAR awareness for explicit single-turn prompts without a ledger scope."""
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


def plan_car_awareness_injection(
    prompt_text: str,
    *,
    hub_root: str | Path,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    backend_thread_id: str | None = None,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    declared_profile: CarContextProfile = DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    target_path: str | None = None,
    initiated_by_ticket_flow: bool = False,
    record_rendered: bool = False,
) -> PlannedPromptInjection:
    """Plan CAR repo awareness through the durable capsule planner."""
    prompt_text = prompt_text or ""
    bundle = build_car_context_bundle(
        declared_profile,
        prompt_text=prompt_text,
        target_path=target_path,
        initiated_by_ticket_flow=initiated_by_ticket_flow,
    )
    capsule = build_car_context_capsule(bundle)
    if capsule is None:
        return PlannedPromptInjection(prompt_text, False)
    try:
        with open_orchestration_sqlite(Path(hub_root)) as conn:
            planned = plan_context_capsules_for_prompt(
                (capsule,),
                ledger=SQLiteContextCapsuleLedger(conn),
                surface_kind=surface_kind,
                surface_key=surface_key,
                managed_thread_id=managed_thread_id,
                backend_thread_id=backend_thread_id,
                repo_id=repo_id,
                worktree_id=worktree_id,
            )
    except Exception:
        _logger.warning(
            "Failed to plan CAR awareness context capsule",
            extra={"surface_kind": surface_kind, "surface_key": surface_key},
            exc_info=True,
        )
        return PlannedPromptInjection(prompt_text, False)
    injection = planned.rendered_text.strip()
    if not injection:
        return PlannedPromptInjection(prompt_text, False, planned.plans)
    if not prompt_text.strip():
        planned_prompt = injection
    else:
        planned_prompt = f"{injection}\n\n{prompt_text}"
    if record_rendered:
        record_planned_prompt_injection(hub_root, planned.rendered_text, planned.plans)
    return PlannedPromptInjection(planned_prompt, True, planned.plans)


def maybe_inject_planned_car_awareness(
    prompt_text: str,
    *,
    hub_root: str | Path,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    backend_thread_id: str | None = None,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    declared_profile: CarContextProfile = DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    target_path: str | None = None,
    initiated_by_ticket_flow: bool = False,
    record_rendered: bool = True,
) -> tuple[str, bool]:
    """Inject CAR repo awareness through the durable capsule planner."""
    planned = plan_car_awareness_injection(
        prompt_text,
        hub_root=hub_root,
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        backend_thread_id=backend_thread_id,
        repo_id=repo_id,
        worktree_id=worktree_id,
        declared_profile=declared_profile,
        target_path=target_path,
        initiated_by_ticket_flow=initiated_by_ticket_flow,
        record_rendered=record_rendered,
    )
    return planned.prompt_text, planned.injected


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


def plan_prompt_writing_hint_injection(
    prompt_text: str,
    *,
    hub_root: str | Path,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    backend_thread_id: str | None = None,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    trigger_text: str | None = None,
    record_rendered: bool = False,
) -> PlannedPromptInjection:
    """Plan prompt-writing guidance through the durable capsule planner."""
    if not prompt_text or not prompt_text.strip():
        return PlannedPromptInjection(prompt_text, False)
    trigger_text = trigger_text if isinstance(trigger_text, str) else prompt_text
    if not _PROMPT_CONTEXT_RE.search(trigger_text):
        return PlannedPromptInjection(prompt_text, False)
    capsule = build_prompt_writing_capsule(PROMPT_WRITING_HINT)
    if PROMPT_WRITING_HINT in prompt_text:
        return PlannedPromptInjection(prompt_text, False)
    try:
        with open_orchestration_sqlite(Path(hub_root)) as conn:
            planned = plan_context_capsules_for_prompt(
                (capsule,),
                ledger=SQLiteContextCapsuleLedger(conn),
                surface_kind=surface_kind,
                surface_key=surface_key,
                managed_thread_id=managed_thread_id,
                backend_thread_id=backend_thread_id,
                repo_id=repo_id,
                worktree_id=worktree_id,
            )
    except Exception:
        _logger.warning(
            "Failed to plan prompt-writing context capsule",
            extra={"surface_kind": surface_kind, "surface_key": surface_key},
            exc_info=True,
        )
        return PlannedPromptInjection(prompt_text, False)
    injection = planned.rendered_text.strip()
    if not injection:
        return PlannedPromptInjection(prompt_text, False, planned.plans)
    separator = "\n" if prompt_text.endswith("\n") else "\n\n"
    planned_prompt = f"{prompt_text}{separator}{injection}"
    if record_rendered:
        record_planned_prompt_injection(hub_root, planned.rendered_text, planned.plans)
    return PlannedPromptInjection(planned_prompt, True, planned.plans)


def maybe_inject_planned_prompt_writing_hint(
    prompt_text: str,
    *,
    hub_root: str | Path,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    backend_thread_id: str | None = None,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    trigger_text: str | None = None,
    record_rendered: bool = True,
) -> tuple[str, bool]:
    """Inject prompt-writing guidance through the durable capsule planner."""
    planned = plan_prompt_writing_hint_injection(
        prompt_text,
        hub_root=hub_root,
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        backend_thread_id=backend_thread_id,
        repo_id=repo_id,
        worktree_id=worktree_id,
        trigger_text=trigger_text,
        record_rendered=record_rendered,
    )
    return planned.prompt_text, planned.injected


def record_planned_prompt_injection(
    hub_root: str | Path,
    rendered_text: str,
    plans: Sequence[ContextCapsuleRenderPlan],
) -> None:
    if not rendered_text.strip() or not plans:
        return
    try:
        with open_orchestration_sqlite(Path(hub_root)) as conn:
            record_context_capsule_renders(SQLiteContextCapsuleLedger(conn), plans)
    except Exception:
        _logger.warning(
            "Failed to record planned prompt context capsules",
            exc_info=True,
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


def plan_worktree_pr_hint_injection(
    prompt_text: str,
    *,
    hub_root: str | Path,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    hint_text: str = WORKTREE_PR_HINT,
    user_input_texts: Sequence[str | None] | None = None,
    record_rendered: bool = False,
) -> PlannedPromptInjection:
    if not prompt_text or not prompt_text.strip():
        return PlannedPromptInjection(prompt_text, False)
    if "worktree.pr_mode" in prompt_text or "PR-mode managed thread" in prompt_text:
        return PlannedPromptInjection(prompt_text, False)
    if not has_user_worktree_pr_hint_request(user_input_texts):
        return PlannedPromptInjection(prompt_text, False)
    payload = {"text": hint_text}
    capsule = ContextCapsule(
        capsule_id="worktree.pr_mode",
        version=1,
        scope=ContextCapsuleScope.THREAD,
        visibility=ContextCapsuleVisibility.MODEL_ONLY,
        source_digest=stable_json_digest(payload),
        expiry=ContextCapsuleExpiry.WHEN_SOURCE_CHANGES,
        reason="worktree_pr_keyword_detected",
        payload=payload,
    )
    try:
        with open_orchestration_sqlite(Path(hub_root)) as conn:
            planned = plan_context_capsules_for_prompt(
                (capsule,),
                ledger=SQLiteContextCapsuleLedger(conn),
                surface_kind=surface_kind,
                surface_key=surface_key,
                managed_thread_id=managed_thread_id,
                record_rendered=record_rendered,
            )
    except Exception:
        _logger.warning(
            "Failed to plan worktree/PR context capsule",
            extra={"surface_kind": surface_kind, "surface_key": surface_key},
            exc_info=True,
        )
        return PlannedPromptInjection(prompt_text, False)
    injection = planned.rendered_text.strip()
    if not injection:
        return PlannedPromptInjection(prompt_text, False, planned.plans)
    separator = "\n" if prompt_text.endswith("\n") else "\n\n"
    planned_prompt = f"{prompt_text}{separator}{injection}"
    if record_rendered:
        record_planned_prompt_injection(hub_root, planned.rendered_text, planned.plans)
    return PlannedPromptInjection(planned_prompt, True, planned.plans)


def maybe_inject_planned_worktree_pr_hint(
    prompt_text: str,
    *,
    hub_root: str | Path,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    hint_text: str = WORKTREE_PR_HINT,
    user_input_texts: Sequence[str | None] | None = None,
    record_rendered: bool = True,
) -> tuple[str, bool]:
    planned = plan_worktree_pr_hint_injection(
        prompt_text,
        hub_root=hub_root,
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        hint_text=hint_text,
        user_input_texts=user_input_texts,
        record_rendered=record_rendered,
    )
    return planned.prompt_text, planned.injected


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
