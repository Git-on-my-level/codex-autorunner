from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from ..integrations.github.reaction_prompts import build_reaction_message
from .pr_bindings import PrBinding
from .scm_events import ScmEvent
from .scm_observability import correlation_id_for_event
from .scm_reaction_types import (
    ReactionIntent,
    ReactionKind,
    ReactionOperationKind,
    ScmReactionConfig,
    stable_reaction_operation_key,
)

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)


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


def _event_payload(event: ScmEvent) -> Mapping[str, Any]:
    return _mapping(event.payload)


def _resolved_repo_slug(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[str]:
    return _normalize_text(event.repo_slug) or (
        _normalize_text(binding.repo_slug) if binding is not None else None
    )


def _resolved_repo_id(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[str]:
    return _normalize_text(event.repo_id) or (
        _normalize_text(binding.repo_id) if binding is not None else None
    )


def _resolved_pr_number(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[int]:
    return binding.pr_number if binding is not None else event.pr_number


def _scm_metadata(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
    reaction_kind: ReactionKind,
) -> dict[str, Any]:
    metadata = {
        "correlation_id": correlation_id_for_event(event),
        "event_id": event.event_id,
        "provider": event.provider,
        "event_type": event.event_type,
        "reaction_kind": reaction_kind,
        "repo_slug": _resolved_repo_slug(event, binding),
        "repo_id": _resolved_repo_id(event, binding),
        "pr_number": _resolved_pr_number(event, binding),
        "binding_id": binding.binding_id if binding is not None else None,
    }
    if binding is not None and binding.thread_target_id is not None:
        metadata["thread_target_id"] = binding.thread_target_id
    return {key: value for key, value in metadata.items() if value is not None}


def _resolve_operation_kind(
    *,
    reaction_kind: ReactionKind,
    event: ScmEvent,
    binding: Optional[PrBinding],
) -> Optional[ReactionOperationKind]:
    if reaction_kind in {"approved_and_green", "merged"}:
        if _resolved_repo_id(event, binding) is not None:
            return "notify_chat"
        if binding is not None and binding.thread_target_id is not None:
            return "enqueue_managed_turn"
        return None
    if binding is not None and binding.thread_target_id is not None:
        return "enqueue_managed_turn"
    if _resolved_repo_id(event, binding) is not None:
        return "notify_chat"
    return None


def _build_publish_payload(
    *,
    reaction_kind: ReactionKind,
    operation_kind: ReactionOperationKind,
    event: ScmEvent,
    binding: Optional[PrBinding],
) -> dict[str, Any]:
    message_text = build_reaction_message(
        reaction_kind=reaction_kind,
        event=event,
        binding=binding,
        operation_kind=operation_kind,
    )
    metadata = {
        "scm": _scm_metadata(event=event, binding=binding, reaction_kind=reaction_kind)
    }

    if operation_kind == "enqueue_managed_turn":
        if binding is None or binding.thread_target_id is None:
            raise ValueError("enqueue_managed_turn reactions require thread_target_id")
        return {
            "correlation_id": correlation_id_for_event(event),
            "thread_target_id": binding.thread_target_id,
            "request": {
                "kind": "message",
                "message_text": message_text,
                "metadata": metadata,
            },
        }

    payload = {
        "correlation_id": correlation_id_for_event(event),
        "delivery": "primary_pma",
        "message": message_text,
        "metadata": metadata,
    }
    repo_id = _resolved_repo_id(event, binding)
    if repo_id is not None:
        payload["repo_id"] = repo_id
    return payload


def _match_reaction_kind(
    event: ScmEvent,
    *,
    binding: Optional[PrBinding],
) -> Optional[ReactionKind]:
    payload = _event_payload(event)

    if event.event_type == "check_run":
        status = _normalize_lower_text(payload.get("status"))
        conclusion = _normalize_lower_text(payload.get("conclusion"))
        if status == "completed" and conclusion in _FAILED_CHECK_CONCLUSIONS:
            return "ci_failed"
        return None

    if event.event_type == "pull_request_review":
        action = _normalize_lower_text(payload.get("action"))
        review_state = _normalize_lower_text(payload.get("review_state"))
        if action != "submitted":
            return None
        if review_state == "changes_requested":
            return "changes_requested"
        # v1 uses an approval review as the ready-to-land signal.
        if review_state == "approved":
            return "approved_and_green"
        return None

    if event.event_type == "issue_comment":
        action = _normalize_lower_text(payload.get("action"))
        author_login = _normalize_lower_text(payload.get("author_login"))
        issue_author_login = _normalize_lower_text(payload.get("issue_author_login"))
        author_type = _normalize_lower_text(payload.get("author_type"))
        if action != "created":
            return None
        if author_type == "bot" or (
            author_login is not None and author_login.endswith("[bot]")
        ):
            return None
        if (
            author_login is not None
            and issue_author_login is not None
            and author_login == issue_author_login
        ):
            return None
        return "review_comment"

    if event.event_type != "pull_request":
        return None

    action = _normalize_lower_text(payload.get("action"))
    merged = payload.get("merged")
    state = _normalize_lower_text(payload.get("state"))
    binding_state = (
        _normalize_lower_text(binding.pr_state) if binding is not None else None
    )
    if action == "closed" and (
        merged is True or state == "merged" or binding_state == "merged"
    ):
        return "merged"
    return None


def route_scm_reactions(
    event: ScmEvent,
    *,
    binding: Optional[PrBinding] = None,
    config: ScmReactionConfig | Mapping[str, Any] | None = None,
) -> list[ReactionIntent]:
    resolved_config = ScmReactionConfig.from_mapping(config)
    reaction_kind = _match_reaction_kind(event, binding=binding)
    if reaction_kind is None or not resolved_config.is_enabled(reaction_kind):
        return []

    operation_kind = _resolve_operation_kind(
        reaction_kind=reaction_kind,
        event=event,
        binding=binding,
    )
    if operation_kind is None:
        return []

    operation_key = stable_reaction_operation_key(
        provider=event.provider,
        event_id=event.event_id,
        reaction_kind=reaction_kind,
        operation_kind=operation_kind,
        repo_slug=_resolved_repo_slug(event, binding),
        repo_id=_resolved_repo_id(event, binding),
        pr_number=_resolved_pr_number(event, binding),
        binding_id=binding.binding_id if binding is not None else None,
        thread_target_id=(
            binding.thread_target_id
            if binding is not None and binding.thread_target_id is not None
            else None
        ),
    )

    return [
        ReactionIntent(
            reaction_kind=reaction_kind,
            operation_kind=operation_kind,
            operation_key=operation_key,
            payload=_build_publish_payload(
                reaction_kind=reaction_kind,
                operation_kind=operation_kind,
                event=event,
                binding=binding,
            ),
            event_id=event.event_id,
            binding_id=binding.binding_id if binding is not None else None,
        )
    ]


__all__ = ["route_scm_reactions"]
