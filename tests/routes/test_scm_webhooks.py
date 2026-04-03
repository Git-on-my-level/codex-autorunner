from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.core.publish_journal import PublishJournalStore
from codex_autorunner.core.scm_events import list_events
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import scm_webhooks as scm_webhooks_module
from codex_autorunner.surfaces.web.routes.scm_webhooks import (
    build_scm_webhook_routes,
)


def _hub_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    if "github" not in cfg:
        cfg["github"] = json.loads(
            json.dumps(cfg.get("repo_defaults", {}).get("github", {}))
        )
    return cfg


def _enable_github_webhooks(
    cfg: dict,
    *,
    drain_inline: bool = False,
    store_raw_payload: bool = False,
    secret: str = "topsecret",
) -> dict:
    cfg["github"]["automation"]["enabled"] = True
    cfg["github"]["automation"]["drain_inline"] = drain_inline
    cfg["github"]["automation"]["webhook_ingress"]["enabled"] = True
    cfg["github"]["automation"]["webhook_ingress"][
        "store_raw_payload"
    ] = store_raw_payload
    cfg["github"]["automation"]["webhook_ingress"]["secret"] = secret
    return cfg


def _enable_github_automation(cfg: dict) -> dict:
    cfg["github"]["automation"]["enabled"] = True
    return cfg


def _build_route_app(
    hub_root: Path,
    *,
    cfg: dict,
    drain_callback=None,
) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(root=hub_root, raw=cfg)
    app.state.logger = logging.getLogger("test.scm_webhooks")
    if drain_callback is not None:
        app.state.scm_webhook_drain_callback = drain_callback
    app.include_router(build_scm_webhook_routes())
    return app


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


def test_scm_webhook_route_is_not_registered_when_disabled(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    cfg = _hub_config()
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    with TestClient(create_hub_app(hub_root)) as client:
        response = client.post("/hub/scm/webhooks/github", content=b"{}")

    assert response.status_code == 404


def test_scm_inspect_endpoints_list_recent_rows(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_automation(_hub_config())

    event = scm_webhooks_module.ScmEventStore(hub_root).record_event(
        event_id="github:delivery-123",
        provider="github",
        event_type="pull_request",
        occurred_at="2026-03-25T10:00:00Z",
        received_at="2026-03-25T10:00:01Z",
        repo_slug="acme/widgets",
        repo_id="99",
        pr_number=42,
        delivery_id="delivery-123",
        correlation_id="scm:github:delivery-123",
        payload={"action": "opened"},
    )
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="99",
        pr_number=42,
        pr_state="open",
        head_branch="feature/inspect",
        base_branch="main",
    )
    reaction = ScmReactionStateStore(hub_root).mark_reaction_delivery_failed(
        binding_id=binding.binding_id,
        reaction_kind="ci_failed",
        fingerprint="fingerprint-1",
        event_id=event.event_id,
        error_text="delivery failed",
        metadata={"repo_slug": "acme/widgets", "pr_number": 42},
    )
    operation, deduped = PublishJournalStore(hub_root).create_operation(
        operation_key="github:comment:inspect",
        operation_kind="github_comment",
        payload={"body": "Inspect output"},
    )
    assert deduped is False

    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        events_response = client.get(
            "/hub/scm/inspect/events",
            params={"provider": "github", "repo_slug": "acme/widgets", "limit": 5},
        )
        bindings_response = client.get(
            "/hub/scm/inspect/bindings",
            params={"repo_slug": "acme/widgets", "pr_state": "open"},
        )
        reactions_response = client.get(
            "/hub/scm/inspect/reactions",
            params={
                "binding_id": binding.binding_id,
                "reaction_kind": "ci_failed",
                "state": "delivery_failed",
            },
        )
        operations_response = client.get(
            "/hub/scm/inspect/publish-operations",
            params={"operation_kind": "github_comment", "limit": 5},
        )

    assert events_response.status_code == 200
    assert events_response.json() == {
        "events": [event.to_dict()],
        "limit": 5,
    }

    assert bindings_response.status_code == 200
    assert bindings_response.json() == {
        "bindings": [binding.to_dict()],
        "limit": 50,
    }

    assert reactions_response.status_code == 200
    assert reactions_response.json() == {
        "reactions": [reaction.to_dict()],
        "limit": 50,
    }

    assert operations_response.status_code == 200
    assert operations_response.json() == {
        "operations": [operation.to_dict()],
        "limit": 5,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/hub/scm/inspect/events",
        "/hub/scm/inspect/bindings",
        "/hub/scm/inspect/reactions",
        "/hub/scm/inspect/publish-operations",
    ],
)
def test_scm_inspect_endpoints_return_disabled_when_automation_off(
    tmp_path: Path, path: str
) -> None:
    hub_root = tmp_path / "hub"
    cfg = _hub_config()
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    with TestClient(create_hub_app(hub_root)) as client:
        response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "SCM automation disabled"}


def test_scm_webhook_persists_event_and_can_drain_inline(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(
        _hub_config(),
        drain_inline=True,
        store_raw_payload=True,
    )
    drained: list[str] = []

    def _drain_callback(_request, event) -> None:
        drained.append(event.event_id)

    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook route",
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
    app = _build_route_app(hub_root, cfg=cfg, drain_callback=_drain_callback)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_id": "github:delivery-1",
        "provider": "github",
        "event_type": "pull_request",
        "repo_slug": "acme/widgets",
        "repo_id": "99",
        "pr_number": 42,
        "delivery_id": "delivery-1",
        "correlation_id": "scm:github:delivery-1",
        "drained_inline": True,
    }

    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert events[0].event_id == "github:delivery-1"
    assert events[0].correlation_id == "scm:github:delivery-1"
    assert events[0].payload["action"] == "opened"
    assert events[0].raw_payload == payload
    assert drained == ["github:delivery-1"]
    with open_orchestration_sqlite(hub_root) as conn:
        audit_row = conn.execute(
            """
            SELECT action_type, payload_json
              FROM orch_audit_entries
             ORDER BY created_at ASC, audit_id ASC
             LIMIT 1
            """
        ).fetchone()
    assert audit_row is not None
    assert audit_row["action_type"] == "scm.ingest"
    assert '"correlation_id":"scm:github:delivery-1"' in str(audit_row["payload_json"])


def test_scm_webhook_accepts_persisted_event_when_inline_drain_fails(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config(), drain_inline=True)

    def _drain_callback(_request, _event) -> None:
        raise RuntimeError("transient drain failure")

    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook route",
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
    app = _build_route_app(hub_root, cfg=cfg, drain_callback=_drain_callback)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_id": "github:delivery-1",
        "provider": "github",
        "event_type": "pull_request",
        "repo_slug": "acme/widgets",
        "repo_id": "99",
        "pr_number": 42,
        "delivery_id": "delivery-1",
        "correlation_id": "scm:github:delivery-1",
        "drained_inline": False,
        "drain_error": "inline_drain_failed",
    }
    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert events[0].event_id == "github:delivery-1"


def test_scm_webhook_accepts_persisted_event_when_ingest_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config(), drain_inline=False)

    def _fail_record(self, *args, **kwargs) -> None:
        _ = self, args, kwargs
        raise sqlite3.OperationalError("audit database unavailable")

    monkeypatch.setattr(scm_webhooks_module.ScmAuditRecorder, "record", _fail_record)

    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook route",
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
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_id": "github:delivery-1",
        "provider": "github",
        "event_type": "pull_request",
        "repo_slug": "acme/widgets",
        "repo_id": "99",
        "pr_number": 42,
        "delivery_id": "delivery-1",
        "correlation_id": "scm:github:delivery-1",
        "drained_inline": False,
        "audit_error": "ingest_audit_failed",
    }
    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert events[0].event_id == "github:delivery-1"


def test_scm_webhook_ignored_requests_return_non_error_without_persisting(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config())
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
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="issue_comment"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "github_event": "issue_comment",
        "delivery_id": "delivery-1",
        "reason": "not_pull_request_comment",
        "detail": "issue_comment is not attached to a pull request",
    }
    assert list_events(hub_root, provider="github", limit=10) == []


def test_scm_webhook_accepts_pull_request_review_comment_event(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config())
    payload = {
        "action": "created",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "reviewer", "id": 8, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook route",
            "state": "open",
            "user": {"login": "pr-author"},
            "updated_at": "2026-03-24T10:01:02+00:00",
        },
        "comment": {
            "id": 444,
            "body": "Please normalize this review-comment webhook too.",
            "html_url": "https://github.com/acme/widgets/pull/42#discussion_r444",
            "created_at": "2026-03-24T14:05:00Z",
            "updated_at": "2026-03-24T14:05:00Z",
            "path": "src/codex_autorunner/integrations/github/webhooks.py",
            "line": 284,
            "user": {"login": "reviewer", "type": "User"},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request_review_comment"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_id": "github:delivery-1",
        "provider": "github",
        "event_type": "pull_request_review_comment",
        "repo_slug": "acme/widgets",
        "repo_id": "99",
        "pr_number": 42,
        "delivery_id": "delivery-1",
        "correlation_id": "scm:github:delivery-1",
        "drained_inline": False,
    }
    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert events[0].event_type == "pull_request_review_comment"
    assert (
        events[0].payload["path"]
        == "src/codex_autorunner/integrations/github/webhooks.py"
    )


def test_scm_webhook_rejects_bad_signature_without_persisting(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config())
    body = b"{}"
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request", signature="sha256=deadbeef"),
        )

    assert response.status_code == 401
    assert response.json() == {
        "status": "rejected",
        "github_event": "pull_request",
        "delivery_id": "delivery-1",
        "reason": "invalid_signature",
        "detail": "GitHub webhook signature did not match",
    }
    assert list_events(hub_root, provider="github", limit=10) == []


def test_scm_webhook_hides_storage_exception_details_from_callers(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config(), store_raw_payload=True)
    cfg["github"]["automation"]["webhook_ingress"]["max_raw_payload_bytes"] = 1
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook route",
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
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )

    assert response.status_code == 413
    assert response.json() == {
        "status": "rejected",
        "reason": "raw_payload_too_large",
        "detail": "SCM event payload exceeds configured storage limits",
    }
    assert list_events(hub_root, provider="github", limit=10) == []


def test_scm_webhook_duplicate_delivery_returns_accepted_deduped(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config(), drain_inline=True)
    drained: list[str] = []

    def _drain_callback(_request, event) -> None:
        drained.append(event.event_id)

    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add webhook route",
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
    app = _build_route_app(hub_root, cfg=cfg, drain_callback=_drain_callback)

    with TestClient(app) as client:
        first = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )
        second = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {
        "status": "accepted",
        "event_id": "github:delivery-1",
        "provider": "github",
        "event_type": "pull_request",
        "repo_slug": "acme/widgets",
        "repo_id": "99",
        "pr_number": 42,
        "delivery_id": "delivery-1",
        "correlation_id": "scm:github:delivery-1",
        "drained_inline": False,
        "deduped": True,
    }
    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert drained == ["github:delivery-1"]


def test_scm_webhook_inline_drain_delegates_to_scm_automation_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config(), drain_inline=True)
    calls: list[tuple[str, object]] = []

    class _ServiceStub:
        def __init__(self, hub_root: Path, *, reaction_config=None) -> None:
            calls.append(("init", hub_root))
            calls.append(
                (
                    "config",
                    dict(reaction_config) if isinstance(reaction_config, dict) else {},
                )
            )

        def ingest_event(self, event) -> None:
            calls.append(("ingest_event", event.event_id))

        def process_now(self, limit: int = 10) -> list[object]:
            calls.append(("process_now", limit))
            return []

    monkeypatch.setattr(scm_webhooks_module, "ScmAutomationService", _ServiceStub)

    payload = {
        "action": "closed",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Wire SCM automation",
            "state": "closed",
            "merged": True,
            "draft": False,
            "html_url": "https://github.com/acme/widgets/pull/42",
            "created_at": "2026-03-24T10:00:00+00:00",
            "updated_at": "2026-03-24T10:01:02+00:00",
            "base": {"ref": "main"},
            "head": {"ref": "feature/scm-automation"},
            "user": {"login": "octocat"},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=_headers(body, event="pull_request"),
        )

    assert response.status_code == 200
    assert calls[0] == ("init", hub_root)
    assert calls[1][0] == "config"
    assert calls[1][1]["enabled"] is True
    assert calls[1][1]["drain_inline"] is True
    assert calls[1][1]["webhook_ingress"]["enabled"] is True
    assert calls[1][1]["webhook_ingress"]["store_raw_payload"] is False
    assert calls[1][1]["webhook_ingress"]["secret"] == "topsecret"
    assert calls[2:] == [
        ("ingest_event", "github:delivery-1"),
        ("process_now", 10),
    ]


def test_scm_webhook_preserves_explicit_correlation_id_header(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = _enable_github_webhooks(_hub_config(), drain_inline=False)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets", "id": 99},
        "sender": {"login": "octocat", "id": 7, "type": "User"},
        "pull_request": {
            "number": 42,
            "title": "Add explicit correlation",
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
    headers = _headers(body, event="pull_request")
    headers["X-Correlation-ID"] = "corr-explicit-123"
    app = _build_route_app(hub_root, cfg=cfg)

    with TestClient(app) as client:
        response = client.post(
            "/hub/scm/webhooks/github",
            content=body,
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["correlation_id"] == "corr-explicit-123"
    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert events[0].correlation_id == "corr-explicit-123"
