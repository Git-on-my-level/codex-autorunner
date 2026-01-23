from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.routes import base as base_routes
from codex_autorunner.routes import flows as flow_routes


def test_ticket_flow_runs_endpoint_returns_empty_list_on_fresh_repo(
    tmp_path, monkeypatch
):
    """Ticket-first: /api/flows/runs must not 404/500 when no runs exist."""

    flow_routes._controller_cache.clear()
    flow_routes._definition_cache.clear()
    flow_routes._active_workers.clear()

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow")
        assert resp.status_code == 200
        assert resp.json() == []


def test_ticket_list_endpoint_returns_empty_list_when_no_tickets(tmp_path, monkeypatch):
    """Ticket-first: /api/flows/ticket_flow/tickets must never fail on empty dir."""

    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tickets"] == []
        assert payload["ticket_dir"].endswith(".codex-autorunner/tickets")


def test_repo_health_is_ok_when_tickets_dir_exists(tmp_path):
    """Repo health should not be gated on legacy flows/docs initialization."""

    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)

    app = FastAPI()
    # Minimal app state for repo_health.
    app.state.config = object()
    app.state.engine = SimpleNamespace(repo_root=Path(tmp_path))

    # build_base_routes requires a static_dir, but /api/repo/health does not use it.
    app.include_router(base_routes.build_base_routes(static_dir=Path(tmp_path)))

    with TestClient(app) as client:
        resp = client.get("/api/repo/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["tickets"]["status"] == "ok"
