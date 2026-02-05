from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.integrations.github.service import RepoInfo
from codex_autorunner.routes import flows as flow_routes


def _reset_state() -> None:
    pass


def test_bootstrap_check_ready(tmp_path, monkeypatch):
    _reset_state()
    issue_path = tmp_path / ".codex-autorunner" / "ISSUE.md"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    issue_path.write_text("ready", encoding="utf-8")
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())
    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/bootstrap-check")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_bootstrap_check_ready_when_tickets_exist(tmp_path, monkeypatch):
    """Even without ISSUE.md, existing tickets should mark repo ready."""

    _reset_state()
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "--\nagent: codex\ndone: false\n--\n", encoding="utf-8"
    )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())
    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/bootstrap-check")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_bootstrap_check_needs_issue_with_github(tmp_path, monkeypatch):
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    class DummyGH:
        def __init__(self, repo_root):
            self.repo_root = repo_root

        def gh_available(self):
            return True

        def gh_authenticated(self):
            return True

        def repo_info(self):
            return RepoInfo(
                name_with_owner="org/repo", url="https://github.com/org/repo"
            )

    monkeypatch.setattr(flow_routes, "GitHubService", DummyGH)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())
    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/bootstrap-check")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "needs_issue"
    assert payload["github_available"] is True
    assert payload["repo"] == "org/repo"


def test_seed_issue_from_plan(tmp_path, monkeypatch):
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())
    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/seed-issue",
            json={"plan_text": "do things"},
        )
    assert resp.status_code == 200
    issue_path = tmp_path / ".codex-autorunner" / "ISSUE.md"
    assert issue_path.exists()
    assert "do things" in issue_path.read_text(encoding="utf-8")


def test_seed_issue_from_github(tmp_path, monkeypatch):
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    class DummyGH:
        def __init__(self, repo_root):
            self.repo_root = repo_root

        def gh_available(self):
            return True

        def gh_authenticated(self):
            return True

        def validate_issue_same_repo(self, issue_ref):
            assert issue_ref == "#5"
            return 5

        def issue_view(self, number: int):
            return {
                "number": number,
                "title": "Example",
                "url": "https://github.com/org/repo/issues/5",
                "state": "open",
                "author": {"login": "alice"},
                "body": "Body text",
            }

        def repo_info(self):
            return RepoInfo(
                name_with_owner="org/repo", url="https://github.com/org/repo"
            )

    monkeypatch.setattr(flow_routes, "GitHubService", DummyGH)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())
    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/seed-issue",
            json={"issue_ref": "#5"},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "github"
    assert payload["issue_number"] == 5
    issue_path = tmp_path / ".codex-autorunner" / "ISSUE.md"
    assert issue_path.exists()
    text = issue_path.read_text(encoding="utf-8")
    assert "# Issue #5" in text
    assert "alice" in text
