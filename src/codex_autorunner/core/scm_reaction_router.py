from __future__ import annotations

import re
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
from .text_utils import _mapping, _normalize_text

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)
_REVIEW_COMMENT_ID_IN_URL = re.compile(r"(?:#discussion_r|/pulls/comments/)(\d+)")
_GITHUB_REVIEW_REACTION_KINDS = frozenset(
    {"changes_requested", "review_comment", "approved_and_green"}
)


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return text.lower() if text is not None else None


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


def _build_review_comment_reaction_payload(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
) -> Optional[dict[str, Any]]:
    payload = _event_payload(event)
    comment_id = _resolve_review_comment_id(payload)
    repo_slug = _resolved_repo_slug(event, binding)
    if comment_id is None or repo_slug is None:
        return None
    reaction_payload: dict[str, Any] = {
        "comment_id": comment_id,
        "content": "eyes",
        "correlation_id": correlation_id_for_event(event),
        "repo_slug": repo_slug,
        "scm": _scm_metadata(
            event=event,
            binding=binding,
            reaction_kind="review_comment",
        ),
    }
    repo_id = _resolved_repo_id(event, binding)
    if repo_id is not None:
        reaction_payload["repo_id"] = repo_id
    if binding is not None:
        reaction_payload["binding_id"] = binding.binding_id
    return reaction_payload


def _review_comment_id_from_url(value: Any) -> Optional[str]:
    url = _normalize_text(value)
    if url is None:
        return None
    match = _REVIEW_COMMENT_ID_IN_URL.search(url)
    if match is None:
        return None
    return match.group(1)


def _resolve_review_comment_id(payload: Mapping[str, Any]) -> Optional[str]:
    raw_comment_id = _normalize_text(payload.get("comment_id"))
    if raw_comment_id is not None and raw_comment_id.isdigit():
        return str(int(raw_comment_id))
    for key in ("html_url", "url"):
        resolved = _review_comment_id_from_url(payload.get(key))
        if resolved is not None:
            return resolved
    return None


def _github_review_author_login(event: ScmEvent) -> Optional[str]:
    payload = _event_payload(event)
    return _normalize_lower_text(payload.get("author_login")) or _normalize_lower_text(
        payload.get("sender_login")
    )


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
        if review_state == "commented":
            return "review_comment"
        # v1 uses an approval review as the ready-to-land signal.
        if review_state == "approved":
            return "approved_and_green"
        return None

    if event.event_type in {"issue_comment", "pull_request_review_comment"}:
        action = _normalize_lower_text(payload.get("action"))
        author_login = _normalize_lower_text(payload.get("author_login"))
        issue_author_login = _normalize_lower_text(payload.get("issue_author_login"))
        author_type = _normalize_lower_text(payload.get("author_type"))
        if action != "created":
            return None
        if (
            author_login is not None
            and issue_author_login is not None
            and author_login == issue_author_login
        ):
            return None
        if event.event_type == "issue_comment" and (
            author_type == "bot"
            or (author_login is not None and author_login.endswith("[bot]"))
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
    if (
        event.provider == "github"
        and reaction_kind in _GITHUB_REVIEW_REACTION_KINDS
        and not resolved_config.github_login_allowed(_github_review_author_login(event))
    ):
        return []

    intents: list[ReactionIntent] = []
    operation_kind = _resolve_operation_kind(
        reaction_kind=reaction_kind,
        event=event,
        binding=binding,
    )
    if operation_kind is not None:
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
        intents.append(
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
        )
    if event.provider == "github" and event.event_type == "pull_request_review_comment":
        reaction_payload = _build_review_comment_reaction_payload(
            event=event,
            binding=binding,
        )
        if reaction_payload is not None:
            intents.append(
                ReactionIntent(
                    reaction_kind=reaction_kind,
                    operation_kind="react_pr_review_comment",
                    operation_key=stable_reaction_operation_key(
                        provider=event.provider,
                        event_id=event.event_id,
                        reaction_kind=reaction_kind,
                        operation_kind="react_pr_review_comment",
                        repo_slug=_resolved_repo_slug(event, binding),
                        repo_id=_resolved_repo_id(event, binding),
                        pr_number=_resolved_pr_number(event, binding),
                        binding_id=binding.binding_id if binding is not None else None,
                        thread_target_id=(
                            binding.thread_target_id
                            if binding is not None
                            and binding.thread_target_id is not None
                            else None
                        ),
                    ),
                    payload=reaction_payload,
                    event_id=event.event_id,
                    binding_id=(binding.binding_id if binding is not None else None),
                )
            )
    return intents


__all__ = ["route_scm_reactions"]
