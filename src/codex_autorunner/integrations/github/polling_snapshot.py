from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from ...core.text_utils import _mapping, _normalize_text

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)
_POST_OPEN_BOOST_UNTIL_SNAPSHOT_KEY = "post_open_boost_until"


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return text.lower() if text is not None else None


def _comment_timestamp(comment: Mapping[str, Any]) -> Optional[str]:
    for key in ("updated_at", "updatedAt", "created_at", "createdAt"):
        timestamp = _normalize_text(comment.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _review_key(review: Mapping[str, Any]) -> str:
    review_id = _normalize_text(review.get("review_id"))
    if review_id is not None:
        return review_id
    submitted_at = _normalize_text(review.get("submitted_at")) or "-"
    author_login = _normalize_text(review.get("author_login")) or "-"
    body = _normalize_text(review.get("body")) or "-"
    return f"{submitted_at}:{author_login}:{body}"


def _check_key(check: Mapping[str, Any]) -> str:
    name = _normalize_text(check.get("name")) or "-"
    conclusion = _normalize_lower_text(check.get("conclusion")) or "-"
    head_sha = _normalize_text(check.get("head_sha")) or "-"
    details_url = _normalize_text(check.get("details_url")) or "-"
    return f"{head_sha}:{name}:{conclusion}:{details_url}"


def _comment_key(comment: Mapping[str, Any]) -> str:
    comment_id = _normalize_text(comment.get("comment_id"))
    if comment_id is not None:
        return comment_id
    timestamp = _comment_timestamp(comment) or "-"
    author_login = _normalize_text(comment.get("author_login")) or "-"
    body = _normalize_text(comment.get("body")) or "-"
    path = _normalize_text(comment.get("path")) or "-"
    line = comment.get("line") if isinstance(comment.get("line"), int) else "-"
    return f"{timestamp}:{author_login}:{path}:{line}:{body}"


def snapshot_map(snapshot: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = snapshot.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def reaction_state_from_pr(pr: Mapping[str, Any]) -> str:
    state = _normalize_lower_text(pr.get("state"))
    is_draft = bool(pr.get("isDraft"))
    if state == "open":
        return "draft" if is_draft else "open"
    return state or "closed"


def initial_post_open_boost_until(
    *,
    binding: Any,
    snapshot: Mapping[str, Any],
    polling_config: Any,
    now: datetime,
    parse_optional_iso: Callable[[Optional[str]], Optional[datetime]],
    iso_after_seconds: Callable[[int], str],
) -> Optional[str]:
    boost_minutes = polling_config.post_open_boost_minutes
    if boost_minutes <= 0:
        return None
    pr_created_at = parse_optional_iso(snapshot.get("pr_created_at"))
    if pr_created_at is not None:
        expires_at = pr_created_at + timedelta(minutes=boost_minutes)
        if expires_at > now:
            return expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return None
    binding_created_at = parse_optional_iso(binding.created_at)
    if binding_created_at is not None:
        expires_at = binding_created_at + timedelta(minutes=boost_minutes)
        if expires_at > now:
            return expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return None
    return iso_after_seconds(boost_minutes * 60)


def snapshot_with_polling_metadata(
    *,
    snapshot: Mapping[str, Any],
    previous_snapshot: Mapping[str, Any] | None = None,
    post_open_boost_until: Optional[str] = None,
) -> dict[str, Any]:
    merged = dict(snapshot)
    inherited_boost = _normalize_text(
        _mapping(previous_snapshot or {}).get(_POST_OPEN_BOOST_UNTIL_SNAPSHOT_KEY)
    )
    resolved_boost = post_open_boost_until or inherited_boost
    if resolved_boost is not None:
        merged[_POST_OPEN_BOOST_UNTIL_SNAPSHOT_KEY] = resolved_boost
    return merged


def _comment_backfill_lower_bound(
    *,
    snapshot: Mapping[str, Any],
    reference_timestamp: str,
    window_seconds: int,
    parse_optional_iso: Callable[[Optional[str]], Optional[datetime]],
) -> Optional[datetime]:
    reference_at = parse_optional_iso(reference_timestamp)
    if reference_at is None:
        return None
    lower_bound = reference_at - timedelta(seconds=max(0, int(window_seconds)))
    pr_created_at = parse_optional_iso(snapshot.get("pr_created_at"))
    if pr_created_at is not None and pr_created_at > lower_bound:
        lower_bound = pr_created_at
    return lower_bound


def _comment_in_backfill_window(
    comment: Mapping[str, Any],
    *,
    lower_bound: Optional[datetime],
    parse_optional_iso: Callable[[Optional[str]], Optional[datetime]],
) -> bool:
    if lower_bound is None:
        return False
    comment_at = parse_optional_iso(_comment_timestamp(comment))
    return comment_at is not None and comment_at >= lower_bound


def snapshot_without_backfilled_comments(
    snapshot: Mapping[str, Any],
    *,
    reference_timestamp: str,
    window_seconds: int,
    parse_optional_iso: Callable[[Optional[str]], Optional[datetime]],
) -> dict[str, Any]:
    lower_bound = _comment_backfill_lower_bound(
        snapshot=snapshot,
        reference_timestamp=reference_timestamp,
        window_seconds=window_seconds,
        parse_optional_iso=parse_optional_iso,
    )
    if lower_bound is None:
        return dict(snapshot)

    previous_snapshot = dict(snapshot)
    issue_comments = {
        key: dict(payload)
        for key, payload in snapshot_map(snapshot, "issue_comments").items()
        if not _comment_in_backfill_window(
            payload,
            lower_bound=lower_bound,
            parse_optional_iso=parse_optional_iso,
        )
    }
    review_thread_comments = {
        key: dict(payload)
        for key, payload in snapshot_map(snapshot, "review_thread_comments").items()
        if not _comment_in_backfill_window(
            payload,
            lower_bound=lower_bound,
            parse_optional_iso=parse_optional_iso,
        )
    }
    if issue_comments:
        previous_snapshot["issue_comments"] = issue_comments
    else:
        previous_snapshot.pop("issue_comments", None)
    if review_thread_comments:
        previous_snapshot["review_thread_comments"] = review_thread_comments
    else:
        previous_snapshot.pop("review_thread_comments", None)
    return previous_snapshot


def build_snapshot(
    *,
    binding: Any,
    service: Any,
) -> dict[str, Any]:
    pr = service.pr_view(number=binding.pr_number, repo_slug=binding.repo_slug)
    head_sha = _normalize_text(pr.get("headRefOid"))
    pr_created_at = _normalize_text(pr.get("createdAt")) or _normalize_text(
        pr.get("created_at")
    )
    pr_state = reaction_state_from_pr(pr)
    pr_author = pr.get("author")
    pr_author_login = (
        _normalize_text(pr_author.get("login"))
        if isinstance(pr_author, Mapping)
        else None
    )
    owner, repo = binding.repo_slug.split("/", 1)
    reviews = service.pr_reviews(owner=owner, repo=repo, number=binding.pr_number)
    checks = service.pr_checks(number=binding.pr_number)
    issue_comments = service.issue_comments(
        owner=owner,
        repo=repo,
        number=binding.pr_number,
    )
    review_threads = service.pr_review_threads(
        owner=owner,
        repo=repo,
        number=binding.pr_number,
    )

    changes_requested_reviews: dict[str, Any] = {}
    for review in reviews:
        if _normalize_lower_text(review.get("review_state")) != "changes_requested":
            continue
        payload = {
            "action": "submitted",
            "review_id": review.get("review_id"),
            "review_state": review.get("review_state"),
            "body": review.get("body"),
            "html_url": review.get("html_url"),
            "author_login": review.get("author_login"),
            "commit_id": review.get("commit_id"),
            "submitted_at": review.get("submitted_at"),
        }
        changes_requested_reviews[_review_key(review)] = {
            key: value for key, value in payload.items() if value is not None
        }

    failed_checks: dict[str, Any] = {}
    for check in checks:
        status = _normalize_lower_text(check.get("status"))
        conclusion = _normalize_lower_text(check.get("conclusion"))
        if status != "completed" or conclusion not in _FAILED_CHECK_CONCLUSIONS:
            continue
        payload = {
            "action": "completed",
            "name": _normalize_text(check.get("name")),
            "status": status,
            "conclusion": conclusion,
            "details_url": _normalize_text(check.get("details_url")),
            "head_sha": _normalize_text(check.get("head_sha")) or head_sha,
        }
        failed_checks[_check_key(payload)] = {
            key: value for key, value in payload.items() if value is not None
        }

    current_issue_comments: dict[str, Any] = {}
    for comment in issue_comments:
        payload = {
            "action": "created",
            "comment_id": _normalize_text(comment.get("comment_id")),
            "body": _normalize_text(comment.get("body")),
            "html_url": _normalize_text(comment.get("html_url")),
            "author_login": _normalize_text(comment.get("author_login")),
            "author_type": _normalize_text(comment.get("author_type")),
            "author_association": _normalize_text(comment.get("author_association")),
            "issue_number": binding.pr_number,
            "issue_author_login": pr_author_login,
            "line": (
                comment.get("line") if isinstance(comment.get("line"), int) else None
            ),
            "path": _normalize_text(comment.get("path")),
            "pull_request_review_id": _normalize_text(
                comment.get("pull_request_review_id")
            ),
            "commit_id": _normalize_text(comment.get("commit_id")),
            "updated_at": _comment_timestamp(comment),
        }
        current_issue_comments[_comment_key(payload)] = {
            key: value for key, value in payload.items() if value is not None
        }

    current_review_thread_comments: dict[str, Any] = {}
    for thread in review_threads:
        comments = thread.get("comments")
        if not isinstance(comments, list):
            continue
        thread_resolved = bool(thread.get("isResolved"))
        for comment in comments:
            if not isinstance(comment, Mapping):
                continue
            payload = {
                "action": "created",
                "comment_id": _normalize_text(comment.get("comment_id")),
                "body": _normalize_text(comment.get("body")),
                "html_url": _normalize_text(comment.get("html_url")),
                "author_login": _normalize_text(comment.get("author_login")),
                "author_type": _normalize_text(comment.get("author_type")),
                "author_association": _normalize_text(
                    comment.get("author_association")
                ),
                "issue_number": binding.pr_number,
                "issue_author_login": pr_author_login,
                "thread_resolved": thread_resolved,
                "line": (
                    comment.get("line")
                    if isinstance(comment.get("line"), int)
                    else None
                ),
                "path": _normalize_text(comment.get("path")),
                "updated_at": _comment_timestamp(comment),
            }
            current_review_thread_comments[_comment_key(payload)] = {
                key: value for key, value in payload.items() if value is not None
            }

    snapshot: dict[str, Any] = {
        "pr_state": pr_state,
        "changes_requested_reviews": changes_requested_reviews,
        "failed_checks": failed_checks,
        "issue_comments": current_issue_comments,
        "review_thread_comments": current_review_thread_comments,
    }
    if head_sha is not None:
        snapshot["head_sha"] = head_sha
    if pr_created_at is not None:
        snapshot["pr_created_at"] = pr_created_at
    return snapshot


__all__ = [
    "build_snapshot",
    "initial_post_open_boost_until",
    "reaction_state_from_pr",
    "snapshot_map",
    "snapshot_with_polling_metadata",
    "snapshot_without_backfilled_comments",
]
