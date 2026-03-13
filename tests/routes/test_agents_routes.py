from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.agents import build_agents_routes


def _build_client(with_supervisors: bool = False) -> TestClient:
    app = FastAPI()
    if with_supervisors:
        app.state.app_server_supervisor = MagicMock()
        app.state.opencode_supervisor = MagicMock()
        app.state.app_server_events = MagicMock()
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


def test_list_agents_returns_capabilities() -> None:
    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.json()
    assert "agents" in data
    assert "default" in data

    agents = data["agents"]
    assert len(agents) >= 1

    for agent in agents:
        assert "id" in agent
        assert "name" in agent
        assert "capabilities" in agent
        assert isinstance(agent["capabilities"], list)


def test_list_agents_includes_expected_capabilities() -> None:
    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.json()
    agents = {agent["id"]: agent for agent in data["agents"]}

    if "codex" in agents:
        codex_caps = agents["codex"]["capabilities"]
        assert "durable_threads" in codex_caps
        assert "message_turns" in codex_caps
        assert "review" in codex_caps
        assert "model_listing" in codex_caps

    if "opencode" in agents:
        opencode_caps = agents["opencode"]["capabilities"]
        assert "durable_threads" in opencode_caps
        assert "message_turns" in opencode_caps
        assert "review" in opencode_caps
