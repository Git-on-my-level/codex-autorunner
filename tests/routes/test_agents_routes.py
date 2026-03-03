from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.agents import build_agents_routes


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(build_agents_routes())
    return TestClient(app)


def test_agent_models_route_rejects_blank_path_agent_segment() -> None:
    client = _build_client()

    response = client.get("/api/agents/%20/models")

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown agent"}


def test_agent_turn_events_route_rejects_blank_path_agent_segment() -> None:
    client = _build_client()

    response = client.get(
        "/api/agents/%20/turns/turn-123/events",
        params={"thread_id": "thread-123"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown agent"}
