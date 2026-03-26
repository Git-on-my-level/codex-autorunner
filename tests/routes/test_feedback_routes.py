from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.feedback_reports import (
    build_feedback_report_routes,
)


def _build_route_app(hub_root: Path) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(root=hub_root, raw={})
    app.state.logger = logging.getLogger("test.feedback_routes")
    app.include_router(build_feedback_report_routes())
    return app


def test_feedback_report_routes_create_and_list_with_filters(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    app = _build_route_app(hub_root)

    first_payload = {
        "report_id": "report-001",
        "repo_id": " repo-1 ",
        "thread_target_id": " thread-1 ",
        "report_kind": " risk ",
        "title": " Missing rollback coverage ",
        "body": "Needs a regression test.\n",
        "evidence": [{"path": "tests/test_feedback.py", "line": 14}],
        "confidence": "0.75",
        "source_kind": " manual review ",
        "source_id": "dispatch-1",
    }
    second_payload = {
        "report_id": "report-002",
        "repo_id": "repo-2",
        "thread_target_id": "thread-2",
        "report_kind": "idea",
        "title": "Add triage summary",
        "body": "Would help spot repeated failures.",
        "source_kind": "operator",
        "source_id": "dispatch-2",
    }

    with TestClient(app) as client:
        create_response = client.post("/hub/feedback-reports", json=first_payload)
        other_response = client.post("/hub/feedback-reports", json=second_payload)
        repo_filtered = client.get(
            "/hub/feedback-reports",
            params={"repo_id": "repo-1", "thread_target_id": "thread-1"},
        )
        kind_filtered = client.get(
            "/hub/feedback-reports",
            params={"report_kind": "idea", "limit": 5},
        )

    assert create_response.status_code == 201
    assert create_response.json() == {
        "report_id": "report-001",
        "repo_id": "repo-1",
        "thread_target_id": "thread-1",
        "report_kind": "risk",
        "title": "Missing rollback coverage",
        "body": "Needs a regression test.",
        "evidence": [{"line": 14, "path": "tests/test_feedback.py"}],
        "confidence": 0.75,
        "source_kind": "manual review",
        "source_id": "dispatch-1",
        "dedupe_key": create_response.json()["dedupe_key"],
        "status": "open",
        "created_at": create_response.json()["created_at"],
        "updated_at": create_response.json()["updated_at"],
    }
    assert len(create_response.json()["dedupe_key"]) == 32
    assert other_response.status_code == 201
    assert other_response.json()["report_id"] == "report-002"

    assert repo_filtered.status_code == 200
    assert repo_filtered.json() == {
        "reports": [create_response.json()],
        "limit": 50,
    }
    assert kind_filtered.status_code == 200
    assert kind_filtered.json() == {
        "reports": [other_response.json()],
        "limit": 5,
    }


def test_feedback_report_create_returns_bad_request_for_invalid_payload(
    tmp_path: Path,
) -> None:
    app = _build_route_app(tmp_path / "hub")

    with TestClient(app) as client:
        response = client.post(
            "/hub/feedback-reports",
            json={
                "report_kind": "risk",
                "title": "Broken confidence",
                "body": "Out of range confidence should fail.",
                "source_kind": "manual",
                "confidence": 2,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "confidence must be between 0 and 1",
    }
