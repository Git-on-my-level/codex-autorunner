from __future__ import annotations

import hashlib
import hmac
import json

from codex_autorunner.integrations.github.webhooks import normalize_github_webhook


def _headers(
    body: bytes,
    *,
    event: str,
    delivery_id: str = "delivery-1",
    secret: str = "topsecret",
    include_signature: bool = True,
    signature: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
    }
    if include_signature:
        headers["X-Hub-Signature-256"] = signature or (
            "sha256="
            + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        )
    return headers


def test_normalize_pull_request_webhook_accepts_supported_payload() -> None:
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook normalizer",
            "state": "open",
            "merged": False,
            "draft": False,
            "html_url": "https://github.com/acme/widgets/pull/42",
            "created_at": "2026-03-24T10:00:00+00:00",
            "updated_at": "2026-03-24T10:01:02+00:00",
            "base": {"ref": "main"},
            "head": {"ref": "feature/webhooks"},
            "user": {"login": "octocat"},
        },
    }
    body = json.dumps(payload).encode("utf-8")

    result = normalize_github_webhook(
        headers=_headers(body, event="pull_request"),
        body=body,
        config={"secret": "topsecret"},
        received_at="2026-03-25T00:00:01+00:00",
    )

    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.event_id == "github:delivery-1"
    assert result.event.provider == "github"
    assert result.event.event_type == "pull_request"
    assert result.event.repo_slug == "acme/widgets"
    assert result.event.repo_id == "99"
    assert result.event.pr_number == 42
    assert result.event.delivery_id == "delivery-1"
    assert result.event.occurred_at == "2026-03-24T10:00:00Z"
    assert result.event.received_at == "2026-03-25T00:00:01Z"
    assert result.event.created_at == "2026-03-25T00:00:01Z"
    assert result.event.payload == {
        "action": "opened",
        "title": "Add webhook normalizer",
        "state": "open",
        "merged": False,
        "draft": False,
        "html_url": "https://github.com/acme/widgets/pull/42",
        "author_login": "octocat",
        "base_ref": "main",
        "head_ref": "feature/webhooks",
        "updated_at": "2026-03-24T10:01:02Z",
        "sender_login": "octocat",
        "sender_id": "7",
        "sender_type": "User",
    }
    assert result.event.raw_payload == payload


def test_normalize_pull_request_review_webhook_accepts_review_state() -> None:
    payload = {
        "action": "submitted",
        "repository": {"full_name": "acme/widgets", "id": "101"},
        "sender": {"login": "reviewer", "id": 8, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook normalizer",
            "state": "open",
            "updated_at": "2026-03-24T12:01:00Z",
        },
        "review": {
            "id": 222,
            "state": "changes_requested",
            "body": "Please add tests.",
            "html_url": "https://github.com/acme/widgets/pull/42#pullrequestreview-222",
            "submitted_at": "2026-03-24T12:00:00+00:00",
            "commit_id": "abc123",
            "user": {"login": "reviewer"},
        },
    }
    body = json.dumps(payload).encode("utf-8")

    result = normalize_github_webhook(
        headers=_headers(body, event="pull_request_review"),
        body=body,
        config={"secret": "topsecret"},
        received_at="2026-03-25T00:00:01Z",
    )

    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.event_type == "pull_request_review"
    assert result.event.pr_number == 42
    assert result.event.occurred_at == "2026-03-24T12:00:00Z"
    assert result.event.payload["review_state"] == "changes_requested"
    assert result.event.payload["review_id"] == "222"
    assert result.event.payload["sender_login"] == "reviewer"


def test_normalize_issue_comment_without_pull_request_is_ignored() -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "issue": {"number": 55},
        "comment": {
            "id": 333,
            "body": "This is an issue comment",
            "html_url": "https://github.com/acme/widgets/issues/55#issuecomment-333",
            "created_at": "2026-03-24T14:00:00Z",
            "updated_at": "2026-03-24T14:00:00Z",
        },
    }
    body = json.dumps(payload).encode("utf-8")

    result = normalize_github_webhook(
        headers=_headers(body, event="issue_comment"),
        body=body,
        config={"secret": "topsecret"},
    )

    assert result.status == "ignored"
    assert result.reason == "not_pull_request_comment"
    assert result.event is None


def test_normalize_webhook_rejects_missing_signature_by_default() -> None:
    body = b"{}"

    result = normalize_github_webhook(
        headers=_headers(body, event="pull_request", include_signature=False),
        body=body,
        config={"secret": "topsecret"},
    )

    assert result.status == "rejected"
    assert result.reason == "missing_signature"
    assert result.event is None


def test_normalize_webhook_rejects_bad_signature() -> None:
    body = b"{}"

    result = normalize_github_webhook(
        headers=_headers(body, event="pull_request", signature="sha256=deadbeef"),
        body=body,
        config={"secret": "topsecret"},
    )

    assert result.status == "rejected"
    assert result.reason == "invalid_signature"
    assert result.event is None


def test_normalize_webhook_accepts_unsigned_when_allowed() -> None:
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "check_run": {
            "id": 444,
            "name": "ci / test",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/acme/widgets/runs/444",
            "details_url": "https://github.com/acme/widgets/runs/444?check_suite_focus=true",
            "head_sha": "deadbeef",
            "started_at": "2026-03-24T15:00:00+00:00",
            "completed_at": "2026-03-24T15:02:00+00:00",
            "pull_requests": [{"number": 42}],
            "app": {"slug": "github-actions"},
        },
    }
    body = json.dumps(payload).encode("utf-8")

    result = normalize_github_webhook(
        headers=_headers(body, event="check_run", include_signature=False),
        body=body,
        config={"verify_signatures": True, "allow_unsigned": True},
        received_at="2026-03-25T00:00:01Z",
    )

    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.event_type == "check_run"
    assert result.event.pr_number == 42
    assert result.event.occurred_at == "2026-03-24T15:02:00Z"
    assert result.event.payload["conclusion"] == "failure"
    assert result.event.payload["app_slug"] == "github-actions"
