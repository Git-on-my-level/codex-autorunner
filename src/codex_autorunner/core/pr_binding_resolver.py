from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .pma_thread_store import PmaThreadStore
from .pr_bindings import PrBinding, PrBindingStore
from .scm_events import ScmEvent

_BRANCH_METADATA_KEYS = ("head_branch", "branch", "git_branch")
_THREAD_TARGET_ID_KEYS = ("thread_target_id", "managed_thread_id")
_CONTEXT_MAPPING_KEYS = ("manual_context", "scm", "scm_context", "context")


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


def _payload_thread_contexts(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    contexts: list[Mapping[str, Any]] = [payload]
    for key in _CONTEXT_MAPPING_KEYS:
        nested = _mapping(payload.get(key))
        if nested:
            contexts.append(nested)
    return tuple(contexts)


def _event_head_branch(event: ScmEvent) -> Optional[str]:
    payload = _mapping(event.payload)
    for context in _payload_thread_contexts(payload):
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


def _thread_branch_matches(thread: Mapping[str, Any], *, head_branch: str) -> bool:
    metadata = _mapping(thread.get("metadata"))
    contexts: list[Mapping[str, Any]] = [metadata]
    for key in _CONTEXT_MAPPING_KEYS:
        nested = _mapping(metadata.get(key))
        if nested:
            contexts.append(nested)

    for context in contexts:
        for key in _BRANCH_METADATA_KEYS:
            candidate = _normalize_text(context.get(key))
            if candidate == head_branch:
                return True
    return False


def _explicit_thread_target_id(
    event: ScmEvent,
    *,
    thread_target_id: Optional[str],
) -> Optional[str]:
    explicit = _normalize_text(thread_target_id)
    if explicit is not None:
        return explicit

    payload = _mapping(event.payload)
    for context in _payload_thread_contexts(payload):
        for key in _THREAD_TARGET_ID_KEYS:
            candidate = _normalize_text(context.get(key))
            if candidate is not None:
                return candidate
    return None


def _resolve_thread_target_id(
    *,
    thread_store: PmaThreadStore,
    event: ScmEvent,
    existing_binding: Optional[PrBinding],
    thread_target_id: Optional[str],
) -> Optional[str]:
    explicit_thread_target_id = _explicit_thread_target_id(
        event, thread_target_id=thread_target_id
    )
    if explicit_thread_target_id is not None:
        if thread_store.get_thread(explicit_thread_target_id) is not None:
            return explicit_thread_target_id

    if existing_binding is not None and existing_binding.thread_target_id is not None:
        return existing_binding.thread_target_id

    repo_id = _normalize_text(event.repo_id)
    head_branch = _event_head_branch(event)
    if repo_id is None or head_branch is None:
        return None

    for thread in thread_store.list_threads(
        status="active", repo_id=repo_id, limit=100
    ):
        candidate_thread_id = _normalize_text(thread.get("managed_thread_id"))
        if candidate_thread_id is None:
            continue
        if _thread_branch_matches(thread, head_branch=head_branch):
            return candidate_thread_id
    return None


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
    thread_store = PmaThreadStore(hub_root)

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
        resolved_thread_target_id = _resolve_thread_target_id(
            thread_store=thread_store,
            event=event,
            existing_binding=existing_branch_binding,
            thread_target_id=thread_target_id,
        )
        if resolved_thread_target_id == existing_branch_binding.thread_target_id:
            return existing_branch_binding
        return binding_store.upsert_binding(
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
    resolved_thread_target_id = _resolve_thread_target_id(
        thread_store=thread_store,
        event=event,
        existing_binding=existing_binding,
        thread_target_id=thread_target_id,
    )

    return binding_store.upsert_binding(
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
