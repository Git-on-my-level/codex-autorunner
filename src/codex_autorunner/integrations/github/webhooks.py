from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional

from ...core.scm_events import ScmEvent
from ...core.time_utils import now_iso

WebhookStatus = Literal["accepted", "ignored", "rejected"]


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def _normalize_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _normalize_iso_timestamp(value: Any) -> Optional[str]:
    text = _normalize_optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        name = _normalize_optional_text(key)
        header_value = _normalize_optional_text(value)
        if name is None or header_value is None:
            continue
        normalized[name.lower()] = header_value
    return normalized


def _event_id_for_request(
    *,
    delivery_id: Optional[str],
    event_name: str,
    body: bytes,
) -> str:
    if delivery_id:
        return f"github:{delivery_id}"
    digest = hashlib.sha256()
    digest.update(event_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body)
    return f"github:body:{digest.hexdigest()}"


def _resolve_sender(payload: Mapping[str, Any]) -> dict[str, Any]:
    sender = payload.get("sender")
    if not isinstance(sender, Mapping):
        return {}
    return {
        "sender_login": _normalize_optional_text(sender.get("login")),
        "sender_id": _normalize_optional_text(sender.get("id")),
        "sender_type": _normalize_optional_text(sender.get("type")),
    }


def _resolve_repository(
    payload: Mapping[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        return None, None
    return (
        _normalize_optional_text(repository.get("full_name")),
        _normalize_optional_text(repository.get("id")),
    )


def _build_pull_request_payload(
    payload: Mapping[str, Any],
) -> tuple[Optional[str], Optional[int], dict[str, Any]]:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, Mapping):
        return None, None, {}
    user = pull_request.get("user")
    author_login = (
        _normalize_optional_text(user.get("login"))
        if isinstance(user, Mapping)
        else None
    )
    normalized = {
        "action": _normalize_optional_text(payload.get("action")),
        "title": _normalize_optional_text(pull_request.get("title")),
        "state": _normalize_optional_text(pull_request.get("state")),
        "merged": _normalize_optional_bool(pull_request.get("merged")),
        "draft": _normalize_optional_bool(pull_request.get("draft")),
        "html_url": _normalize_optional_text(pull_request.get("html_url")),
        "author_login": author_login,
        "base_ref": _normalize_optional_text(
            pull_request.get("base", {}).get("ref")
            if isinstance(pull_request.get("base"), Mapping)
            else None
        ),
        "head_ref": _normalize_optional_text(
            pull_request.get("head", {}).get("ref")
            if isinstance(pull_request.get("head"), Mapping)
            else None
        ),
        "merged_at": _normalize_iso_timestamp(pull_request.get("merged_at")),
        "closed_at": _normalize_iso_timestamp(pull_request.get("closed_at")),
        "updated_at": _normalize_iso_timestamp(pull_request.get("updated_at")),
    }
    return (
        _resolve_pull_request_occurred_at(payload),
        _normalize_optional_int(pull_request.get("number")),
        _compact_payload(normalized),
    )


def _resolve_pull_request_occurred_at(payload: Mapping[str, Any]) -> Optional[str]:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, Mapping):
        return None
    action = _normalize_optional_text(payload.get("action")) or ""
    merged = bool(pull_request.get("merged"))
    if action == "opened":
        return _first_timestamp(
            pull_request.get("created_at"), pull_request.get("updated_at")
        )
    if action == "closed" and merged:
        return _first_timestamp(
            pull_request.get("merged_at"),
            pull_request.get("closed_at"),
            pull_request.get("updated_at"),
        )
    if action == "closed":
        return _first_timestamp(
            pull_request.get("closed_at"), pull_request.get("updated_at")
        )
    return _first_timestamp(
        pull_request.get("updated_at"),
        pull_request.get("created_at"),
        pull_request.get("closed_at"),
        pull_request.get("merged_at"),
    )


def _build_pull_request_review_payload(
    payload: Mapping[str, Any],
) -> tuple[Optional[str], Optional[int], dict[str, Any]]:
    review = payload.get("review")
    pull_request = payload.get("pull_request")
    if not isinstance(review, Mapping) or not isinstance(pull_request, Mapping):
        return None, None, {}
    user = review.get("user")
    author_login = (
        _normalize_optional_text(user.get("login"))
        if isinstance(user, Mapping)
        else None
    )
    normalized = {
        "action": _normalize_optional_text(payload.get("action")),
        "review_id": _normalize_optional_text(review.get("id")),
        "review_state": _normalize_optional_text(review.get("state")),
        "body": _normalize_optional_text(review.get("body")),
        "html_url": _normalize_optional_text(review.get("html_url")),
        "author_login": author_login,
        "commit_id": _normalize_optional_text(review.get("commit_id")),
        "submitted_at": _normalize_iso_timestamp(review.get("submitted_at")),
        "pull_request_title": _normalize_optional_text(pull_request.get("title")),
        "pull_request_state": _normalize_optional_text(pull_request.get("state")),
    }
    occurred_at = _first_timestamp(
        review.get("submitted_at"),
        review.get("updated_at"),
        pull_request.get("updated_at"),
    )
    return (
        occurred_at,
        _normalize_optional_int(pull_request.get("number")),
        _compact_payload(normalized),
    )


def _build_issue_comment_payload(
    payload: Mapping[str, Any],
) -> tuple[
    WebhookStatus,
    Optional[str],
    Optional[int],
    dict[str, Any],
    Optional[str],
    Optional[str],
]:
    issue = payload.get("issue")
    comment = payload.get("comment")
    if not isinstance(issue, Mapping) or not isinstance(comment, Mapping):
        return (
            "rejected",
            None,
            None,
            {},
            "invalid_payload",
            "issue_comment requires issue and comment objects",
        )
    if not isinstance(issue.get("pull_request"), Mapping):
        return (
            "ignored",
            None,
            None,
            {},
            "not_pull_request_comment",
            "issue_comment is not attached to a pull request",
        )
    user = comment.get("user")
    author_login = (
        _normalize_optional_text(user.get("login"))
        if isinstance(user, Mapping)
        else None
    )
    author_type = (
        _normalize_optional_text(user.get("type"))
        if isinstance(user, Mapping)
        else None
    )
    issue_author = issue.get("user")
    issue_author_login = (
        _normalize_optional_text(issue_author.get("login"))
        if isinstance(issue_author, Mapping)
        else None
    )
    normalized = {
        "action": _normalize_optional_text(payload.get("action")),
        "comment_id": _normalize_optional_text(comment.get("id")),
        "body": _normalize_optional_text(comment.get("body")),
        "html_url": _normalize_optional_text(comment.get("html_url")),
        "author_login": author_login,
        "author_type": author_type,
        "author_association": _normalize_optional_text(
            comment.get("author_association")
        ),
        "issue_number": _normalize_optional_int(issue.get("number")),
        "issue_author_login": issue_author_login,
        "line": _normalize_optional_int(comment.get("line")),
        "path": _normalize_optional_text(comment.get("path")),
        "updated_at": _normalize_iso_timestamp(comment.get("updated_at")),
    }
    occurred_at = _first_timestamp(comment.get("updated_at"), comment.get("created_at"))
    return (
        "accepted",
        occurred_at,
        _normalize_optional_int(issue.get("number")),
        _compact_payload(normalized),
        None,
        None,
    )


def _build_check_run_payload(
    payload: Mapping[str, Any],
) -> tuple[Optional[str], Optional[int], dict[str, Any]]:
    check_run = payload.get("check_run")
    if not isinstance(check_run, Mapping):
        return None, None, {}
    pull_requests = check_run.get("pull_requests")
    pr_number = None
    if isinstance(pull_requests, list):
        for item in pull_requests:
            if isinstance(item, Mapping):
                pr_number = _normalize_optional_int(item.get("number"))
                if pr_number is not None:
                    break
    app = check_run.get("app")
    app_slug = (
        _normalize_optional_text(app.get("slug")) if isinstance(app, Mapping) else None
    )
    normalized = {
        "action": _normalize_optional_text(payload.get("action")),
        "check_run_id": _normalize_optional_text(check_run.get("id")),
        "name": _normalize_optional_text(check_run.get("name")),
        "status": _normalize_optional_text(check_run.get("status")),
        "conclusion": _normalize_optional_text(check_run.get("conclusion")),
        "html_url": _normalize_optional_text(check_run.get("html_url")),
        "details_url": _normalize_optional_text(check_run.get("details_url")),
        "head_sha": _normalize_optional_text(check_run.get("head_sha")),
        "app_slug": app_slug,
        "started_at": _normalize_iso_timestamp(check_run.get("started_at")),
        "completed_at": _normalize_iso_timestamp(check_run.get("completed_at")),
    }
    occurred_at = _first_timestamp(
        check_run.get("completed_at"),
        check_run.get("started_at"),
        check_run.get("created_at"),
        check_run.get("updated_at"),
    )
    return occurred_at, pr_number, _compact_payload(normalized)


def _first_timestamp(*candidates: Any) -> Optional[str]:
    for candidate in candidates:
        normalized = _normalize_iso_timestamp(candidate)
        if normalized is not None:
            return normalized
    return None


def _compact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class GitHubWebhookConfig:
    secret: Optional[str] = None
    verify_signatures: bool = True
    allow_unsigned: bool = False

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "GitHubWebhookConfig":
        if value is None:
            return cls()
        secret = _normalize_optional_text(value.get("secret"))
        verify_signatures = value.get("verify_signatures")
        allow_unsigned = value.get("allow_unsigned")
        return cls(
            secret=secret,
            verify_signatures=(
                bool(verify_signatures) if isinstance(verify_signatures, bool) else True
            ),
            allow_unsigned=(
                bool(allow_unsigned) if isinstance(allow_unsigned, bool) else False
            ),
        )


@dataclass(frozen=True)
class GitHubWebhookResult:
    status: WebhookStatus
    github_event: Optional[str] = None
    delivery_id: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    event: Optional[ScmEvent] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "github_event": self.github_event,
            "delivery_id": self.delivery_id,
            "reason": self.reason,
            "detail": self.detail,
            "meta": dict(self.meta),
        }
        if self.event is not None:
            payload["event"] = self.event.to_dict()
        return payload


def normalize_github_webhook(
    *,
    headers: Mapping[str, Any],
    body: bytes,
    config: GitHubWebhookConfig | Mapping[str, Any] | None = None,
    received_at: Optional[str] = None,
) -> GitHubWebhookResult:
    resolved_config = (
        config
        if isinstance(config, GitHubWebhookConfig)
        else GitHubWebhookConfig.from_mapping(config)
    )
    resolved_received_at = _normalize_iso_timestamp(received_at) or now_iso()
    normalized_headers = _normalize_headers(headers)
    event_name = _normalize_optional_text(normalized_headers.get("x-github-event"))
    delivery_id = _normalize_optional_text(normalized_headers.get("x-github-delivery"))
    signature = _normalize_optional_text(normalized_headers.get("x-hub-signature-256"))

    signature_error = _verify_signature(
        body=body,
        signature=signature,
        config=resolved_config,
    )
    if signature_error is not None:
        return GitHubWebhookResult(
            status="rejected",
            github_event=event_name,
            delivery_id=delivery_id,
            reason=signature_error[0],
            detail=signature_error[1],
        )

    if event_name is None:
        return GitHubWebhookResult(
            status="rejected",
            delivery_id=delivery_id,
            reason="missing_event_name",
            detail="Missing X-GitHub-Event header",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return GitHubWebhookResult(
            status="rejected",
            github_event=event_name,
            delivery_id=delivery_id,
            reason="invalid_json",
            detail="Webhook body must be a valid JSON object",
        )
    if not isinstance(payload, Mapping):
        return GitHubWebhookResult(
            status="rejected",
            github_event=event_name,
            delivery_id=delivery_id,
            reason="invalid_payload",
            detail="Webhook body must decode to a JSON object",
        )

    repo_slug, repo_id = _resolve_repository(payload)
    sender_fields = _resolve_sender(payload)
    meta = {**sender_fields}
    normalized = _normalize_supported_event(
        event_name=event_name,
        payload=payload,
    )
    if normalized.status != "accepted":
        return GitHubWebhookResult(
            status=normalized.status,
            github_event=event_name,
            delivery_id=delivery_id,
            reason=normalized.reason,
            detail=normalized.detail,
            meta=meta,
        )

    event = ScmEvent(
        event_id=_event_id_for_request(
            delivery_id=delivery_id,
            event_name=event_name,
            body=body,
        ),
        provider="github",
        event_type=event_name,
        occurred_at=normalized.occurred_at or resolved_received_at,
        received_at=resolved_received_at,
        created_at=resolved_received_at,
        repo_slug=repo_slug,
        repo_id=repo_id,
        pr_number=normalized.pr_number,
        delivery_id=delivery_id,
        payload={**normalized.payload, **sender_fields},
        raw_payload=dict(payload),
    )
    return GitHubWebhookResult(
        status="accepted",
        github_event=event_name,
        delivery_id=delivery_id,
        event=event,
        meta=meta,
    )


@dataclass(frozen=True)
class _NormalizedEvent:
    status: WebhookStatus
    occurred_at: Optional[str] = None
    pr_number: Optional[int] = None
    payload: dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    detail: Optional[str] = None


def _normalize_supported_event(
    *,
    event_name: str,
    payload: Mapping[str, Any],
) -> _NormalizedEvent:
    if event_name == "pull_request":
        occurred_at, pr_number, normalized_payload = _build_pull_request_payload(
            payload
        )
        if not normalized_payload:
            return _NormalizedEvent(
                status="rejected",
                reason="invalid_payload",
                detail="pull_request event requires a pull_request object",
            )
        return _NormalizedEvent(
            status="accepted",
            occurred_at=occurred_at,
            pr_number=pr_number,
            payload=normalized_payload,
        )
    if event_name == "pull_request_review":
        occurred_at, pr_number, normalized_payload = _build_pull_request_review_payload(
            payload
        )
        if not normalized_payload:
            return _NormalizedEvent(
                status="rejected",
                reason="invalid_payload",
                detail="pull_request_review event requires review and pull_request objects",
            )
        return _NormalizedEvent(
            status="accepted",
            occurred_at=occurred_at,
            pr_number=pr_number,
            payload=normalized_payload,
        )
    if event_name == "issue_comment":
        status, occurred_at, pr_number, normalized_payload, reason, detail = (
            _build_issue_comment_payload(payload)
        )
        return _NormalizedEvent(
            status=status,
            occurred_at=occurred_at,
            pr_number=pr_number,
            payload=normalized_payload,
            reason=reason,
            detail=detail,
        )
    if event_name == "check_run":
        occurred_at, pr_number, normalized_payload = _build_check_run_payload(payload)
        if not normalized_payload:
            return _NormalizedEvent(
                status="rejected",
                reason="invalid_payload",
                detail="check_run event requires a check_run object",
            )
        return _NormalizedEvent(
            status="accepted",
            occurred_at=occurred_at,
            pr_number=pr_number,
            payload=normalized_payload,
        )
    return _NormalizedEvent(
        status="ignored",
        reason="unsupported_event",
        detail=f"GitHub event '{event_name}' is not normalized",
    )


def _verify_signature(
    *,
    body: bytes,
    signature: Optional[str],
    config: GitHubWebhookConfig,
) -> Optional[tuple[str, str]]:
    if not config.verify_signatures:
        return None
    if signature is None:
        if config.allow_unsigned:
            return None
        return "missing_signature", "Missing X-Hub-Signature-256 header"
    if config.secret is None:
        return (
            "missing_secret",
            "GitHub webhook secret is required for signature verification",
        )
    prefix = "sha256="
    if not signature.startswith(prefix):
        return "invalid_signature", "X-Hub-Signature-256 must use the sha256= prefix"
    digest = hmac.new(
        config.secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    expected = f"{prefix}{digest}"
    if not hmac.compare_digest(signature, expected):
        return "invalid_signature", "GitHub webhook signature did not match"
    return None


__all__ = [
    "GitHubWebhookConfig",
    "GitHubWebhookResult",
    "normalize_github_webhook",
]
