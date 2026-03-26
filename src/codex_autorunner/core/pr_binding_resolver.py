from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .pr_binding_runtime import (
    explicit_thread_target_id,
    resolve_thread_target_id,
    thread_contexts,
    upsert_pr_binding,
)
from .pr_bindings import PrBinding, PrBindingStore
from .scm_events import ScmEvent


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return text.lower() if text is not None else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_head_branch(event: ScmEvent) -> Optional[str]:
    payload = _mapping(event.payload)
    for context in thread_contexts(payload):
        for key in ("head_ref", "head_branch", "branch"):
            branch = _normalize_text(context.get(key))
            if branch is not None:
                return branch
    raw_pull_request = _mapping(_mapping(event.raw_payload).get("pull_request"))
    raw_head = _mapping(raw_pull_request.get("head"))
    return _normalize_text(raw_head.get("ref"))


def _event_base_branch(event: ScmEvent) -> Optional[str]:
    payload = _mapping(event.payload)
    for key in ("base_ref", "base_branch"):
        branch = _normalize_text(payload.get(key))
        if branch is not None:
            return branch
    raw_pull_request = _mapping(_mapping(event.raw_payload).get("pull_request"))
    raw_base = _mapping(raw_pull_request.get("base"))
    return _normalize_text(raw_base.get("ref"))


def _event_pr_state(event: ScmEvent, *, existing_state: Optional[str] = None) -> str:
    payload = _mapping(event.payload)
    merged = payload.get("merged")
    draft = payload.get("draft")
    action = _normalize_lower_text(payload.get("action"))
    state = _normalize_lower_text(payload.get("state"))

    raw_pull_request = _mapping(_mapping(event.raw_payload).get("pull_request"))
    if state is None:
        state = _normalize_lower_text(raw_pull_request.get("state"))
    if merged is None:
        merged = raw_pull_request.get("merged")
    if draft is None:
        draft = raw_pull_request.get("draft")

    if merged is True:
        return "merged"
    if state == "closed" or action == "closed":
        return "closed"
    if draft is True:
        return "draft"
    if state in {"open", "draft", "closed", "merged"}:
        return state
    return existing_state or "open"


def _explicit_thread_target_id(
    event: ScmEvent,
    *,
    thread_target_id: Optional[str],
) -> Optional[str]:
    explicit = _normalize_text(thread_target_id)
    if explicit is not None:
        return explicit

    return explicit_thread_target_id(_mapping(event.payload))


def resolve_binding_for_scm_event(
    hub_root: Path,
    event: ScmEvent,
    *,
    thread_target_id: Optional[str] = None,
) -> Optional[PrBinding]:
    repo_slug = _normalize_text(event.repo_slug)
    pr_number = event.pr_number
    if repo_slug is None:
        return None

    provider = event.provider
    binding_store = PrBindingStore(hub_root)
    if pr_number is None:
        head_branch = _event_head_branch(event)
        if head_branch is None:
            return None
        existing_branch_binding = binding_store.find_active_binding_for_branch(
            provider=provider,
            repo_slug=repo_slug,
            branch_name=head_branch,
        )
        if existing_branch_binding is None:
            return None
        resolved_thread_target_id = resolve_thread_target_id(
            hub_root=hub_root,
            repo_id=_normalize_text(event.repo_id),
            head_branch=head_branch,
            existing_binding=existing_branch_binding,
            thread_target_id=_explicit_thread_target_id(
                event, thread_target_id=thread_target_id
            ),
        )
        if resolved_thread_target_id == existing_branch_binding.thread_target_id:
            return existing_branch_binding
        return upsert_pr_binding(
            hub_root,
            provider=provider,
            repo_slug=repo_slug,
            repo_id=event.repo_id,
            pr_number=existing_branch_binding.pr_number,
            pr_state=existing_branch_binding.pr_state,
            head_branch=existing_branch_binding.head_branch,
            base_branch=existing_branch_binding.base_branch,
            thread_target_id=resolved_thread_target_id,
        )

    existing_binding = binding_store.get_binding_by_pr(
        provider=provider,
        repo_slug=repo_slug,
        pr_number=pr_number,
    )
    resolved_thread_target_id = resolve_thread_target_id(
        hub_root=hub_root,
        repo_id=_normalize_text(event.repo_id),
        head_branch=_event_head_branch(event),
        existing_binding=existing_binding,
        thread_target_id=_explicit_thread_target_id(
            event, thread_target_id=thread_target_id
        ),
    )

    return upsert_pr_binding(
        hub_root,
        provider=provider,
        repo_slug=repo_slug,
        repo_id=event.repo_id,
        pr_number=pr_number,
        pr_state=_event_pr_state(
            event,
            existing_state=existing_binding.pr_state if existing_binding else None,
        ),
        head_branch=_event_head_branch(event),
        base_branch=_event_base_branch(event),
        thread_target_id=resolved_thread_target_id,
    )


__all__ = ["resolve_binding_for_scm_event"]
