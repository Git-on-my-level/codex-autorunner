from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.scm_events import list_events
from codex_autorunner.server import create_hub_app
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
        "drained_inline": True,
    }

    events = list_events(hub_root, provider="github", limit=10)
    assert len(events) == 1
    assert events[0].event_id == "github:delivery-1"
    assert events[0].payload["action"] == "opened"
    assert events[0].raw_payload == payload
    assert drained == ["github:delivery-1"]


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
