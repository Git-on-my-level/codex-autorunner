from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from .pr_bindings import PrBinding
from .scm_events import ScmEvent
from .scm_reaction_types import ReactionKind, ReactionOperationKind
from .text_utils import _mapping


def _collapse_whitespace(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _collapse_whitespace(value)
    return text.lower() if text is not None else None


def _event_payload(event: ScmEvent) -> Mapping[str, Any]:
    return _mapping(event.payload)


def _resolved_repo_slug(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[str]:
    return _collapse_whitespace(event.repo_slug) or (
        _collapse_whitespace(binding.repo_slug) if binding is not None else None
    )


def _resolved_pr_number(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[int]:
    return binding.pr_number if binding is not None else event.pr_number


def _reaction_subject(event: ScmEvent, binding: Optional[PrBinding]) -> str:
    repo_slug = _resolved_repo_slug(event, binding) or event.provider
    pr_number = _resolved_pr_number(event, binding)
    if pr_number is None:
        return repo_slug
    return f"{repo_slug}#{pr_number}"


def _ensure_sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _trimmed_summary(value: Any, *, limit: int = 120) -> Optional[str]:
    text = _collapse_whitespace(value)
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _join_message(summary: str, next_step: Optional[str] = None) -> str:
    sentence = _ensure_sentence(summary)
    if next_step is None:
        return sentence
    return f"{sentence} {_ensure_sentence(next_step)}"


def _check_detail(payload: Mapping[str, Any]) -> Optional[str]:
    check_name = _collapse_whitespace(payload.get("name"))
    conclusion = _normalize_lower_text(payload.get("conclusion"))
    if check_name and conclusion:
        return f"{check_name} ({conclusion})"
    if check_name:
        return check_name
    if conclusion:
        return conclusion
    return None


def _reviewer_login(payload: Mapping[str, Any]) -> Optional[str]:
    return _collapse_whitespace(payload.get("author_login"))


def _comment_location(payload: Mapping[str, Any]) -> Optional[str]:
    path = _collapse_whitespace(payload.get("path"))
    line = payload.get("line")
    if path is None:
        return None
    if isinstance(line, int):
        return f"{path}:{line}"
    return path


def build_ci_failed_message(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
    operation_kind: ReactionOperationKind,
) -> str:
    payload = _event_payload(event)
    subject = _reaction_subject(event, binding)
    detail = _check_detail(payload)
    if detail is not None:
        summary = f"CI failed for {subject}: {detail}"
    else:
        summary = f"CI failed for {subject}"
    if operation_kind == "enqueue_managed_turn":
        return _join_message(summary, "Inspect the failing check and push a fix")
    return _ensure_sentence(summary)


def build_changes_requested_message(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
    operation_kind: ReactionOperationKind,
) -> str:
    payload = _event_payload(event)
    subject = _reaction_subject(event, binding)
    reviewer_login = _reviewer_login(payload)
    review_summary = _trimmed_summary(payload.get("body"))

    summary = f"Changes requested on {subject}"
    if reviewer_login is not None:
        summary = f"{summary} by {reviewer_login}"
    if review_summary is not None:
        summary = f"{summary}: {review_summary}"

    if operation_kind == "enqueue_managed_turn":
        return _join_message(
            summary,
            "Address the feedback and reply after updating the PR",
        )
    return _ensure_sentence(summary)


def build_approved_and_green_message(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
    operation_kind: ReactionOperationKind,
) -> str:
    del operation_kind
    payload = _event_payload(event)
    subject = _reaction_subject(event, binding)
    reviewer_login = _reviewer_login(payload)
    if reviewer_login is not None:
        return f"{subject} is approved and ready to land ({reviewer_login})."
    return f"{subject} is approved and ready to land."


def build_review_comment_message(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
    operation_kind: ReactionOperationKind,
) -> str:
    payload = _event_payload(event)
    subject = _reaction_subject(event, binding)
    if operation_kind == "enqueue_managed_turn":
        return _join_message(
            f"New PR review feedback arrived on {subject}",
            "Inspect the latest review comments on the PR, address the feedback, and reply on the PR after updating the branch",
        )
    commenter_login = _reviewer_login(payload)
    comment_summary = _trimmed_summary(payload.get("body"))
    location = _comment_location(payload)

    summary = f"New PR comment on {subject}"
    if commenter_login is not None:
        summary = f"{summary} from {commenter_login}"
    if location is not None:
        summary = f"{summary} at {location}"
    if comment_summary is not None:
        summary = f"{summary}: {comment_summary}"
    return _ensure_sentence(summary)


def build_merged_message(
    *,
    event: ScmEvent,
    binding: Optional[PrBinding],
    operation_kind: ReactionOperationKind,
) -> str:
    del operation_kind
    subject = _reaction_subject(event, binding)
    return f"{subject} was merged."


def build_reaction_message(
    *,
    reaction_kind: ReactionKind,
    event: ScmEvent,
    binding: Optional[PrBinding],
    operation_kind: ReactionOperationKind,
) -> str:
    builders = {
        "ci_failed": build_ci_failed_message,
        "changes_requested": build_changes_requested_message,
        "review_comment": build_review_comment_message,
        "approved_and_green": build_approved_and_green_message,
        "merged": build_merged_message,
    }
    builder = builders[reaction_kind]
    return builder(event=event, binding=binding, operation_kind=operation_kind)


__all__ = [
    "build_approved_and_green_message",
    "build_changes_requested_message",
    "build_ci_failed_message",
    "build_merged_message",
    "build_reaction_message",
    "build_review_comment_message",
]
