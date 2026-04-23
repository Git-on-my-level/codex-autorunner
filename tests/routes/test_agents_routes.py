from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.integrations.chat.agents import ChatAgentProfileOption
from codex_autorunner.surfaces.web.routes.agents import build_agents_routes

_TEST_REPO_ROOT = "/workspace/test-repo"


def _build_client(with_supervisors: bool = False) -> TestClient:
    app = FastAPI()
    if with_supervisors:
        app.state.app_server_supervisor = MagicMock()
        app.state.opencode_supervisor = MagicMock()
        app.state.app_server_events = MagicMock()
        app.state.config = SimpleNamespace(
            agent_binary=lambda _agent_id: "zeroclaw",
        )
        app.state.engine = SimpleNamespace(repo_root=_TEST_REPO_ROOT)
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


def test_agent_turn_events_route_preserves_resume_offset_for_codex() -> None:
    class FakeEvents:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        def stream(self, thread_id: str, turn_id: str, *, after_id: int = 0):
            self.calls.append((thread_id, turn_id, after_id))

            async def _stream():
                if False:
                    yield b""

            return _stream()

    app = FastAPI()
    app.state.app_server_events = FakeEvents()
    app.state.engine = SimpleNamespace(repo_root=_TEST_REPO_ROOT)
    app.include_router(build_agents_routes())

    with TestClient(app) as client:
        response = client.get(
            "/api/agents/codex/turns/turn-123/events",
            params={"thread_id": "thread-123", "since_event_id": 7},
        )

    assert response.status_code == 200
    assert app.state.app_server_events.calls == [("thread-123", "turn-123", 7)]


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

    if "zeroclaw" in agents:
        zeroclaw_caps = agents["zeroclaw"]["capabilities"]
        assert "durable_threads" in zeroclaw_caps
        assert "message_turns" in zeroclaw_caps
        assert "active_thread_discovery" in zeroclaw_caps
        assert "event_streaming" in zeroclaw_caps


def test_list_agents_fallback_does_not_advertise_unavailable_capabilities(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.agents.get_available_agents",
        lambda _state: {},
    )
    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.json()
    assert data["agents"] == [
        {
            "id": "codex",
            "name": "Codex",
            "protocol_version": "2.0",
            "capabilities": [],
        }
    ]


def test_list_agents_omits_zeroclaw_when_runtime_is_incompatible(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.zeroclaw_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "incompatible",
                "version": "zeroclaw 0.2.0",
                "launch_mode": None,
                "message": "incompatible",
                "fix": "Install a compatible ZeroClaw build.",
            },
        )(),
    )
    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.json()
    assert "zeroclaw" not in {agent["id"] for agent in data["agents"]}


def test_list_agents_includes_hermes_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "hermes 0.1.0",
                "launch_mode": "binary",
                "message": "ready",
                "fix": None,
            },
        )(),
    )

    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.json()
    agents = {agent["id"]: agent for agent in data["agents"]}

    if "hermes" in agents:
        hermes_caps = agents["hermes"]["capabilities"]
        assert "durable_threads" in hermes_caps
        assert "message_turns" in hermes_caps
        assert "active_thread_discovery" in hermes_caps
        assert "interrupt" in hermes_caps
        assert "event_streaming" in hermes_caps
        assert "approvals" in hermes_caps
        assert "model_listing" not in hermes_caps


def test_models_endpoint_returns_capability_error_for_hermes(monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "hermes 0.1.0",
                "launch_mode": "binary",
                "message": "ready",
                "fix": None,
            },
        )(),
    )

    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents/hermes/models")

    assert response.status_code == 400
    data = response.json()
    assert "model_listing" in data["detail"]
    assert "hermes" in data["detail"]


def test_list_agents_includes_configured_profiles(monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "hermes 0.1.0",
                "launch_mode": "binary",
                "message": "ready",
                "fix": None,
            },
        )(),
    )

    app = FastAPI()
    app.state.app_server_supervisor = MagicMock()
    app.state.opencode_supervisor = MagicMock()
    app.state.app_server_events = MagicMock()
    app.state.config = SimpleNamespace(
        agent_binary=lambda _agent_id, profile=None: (
            "hermes" if profile else "zeroclaw"
        ),
        agent_profiles=lambda agent_id: (
            {"m4": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_default_profile=lambda agent_id: "m4" if agent_id == "hermes" else None,
    )
    app.state.engine = SimpleNamespace(repo_root=_TEST_REPO_ROOT)
    app.include_router(build_agents_routes())

    with TestClient(app) as client:
        response = client.get("/api/agents")

    assert response.status_code == 200
    agents = {agent["id"]: agent for agent in response.json()["agents"]}
    assert agents["hermes"]["default_profile"] == "m4"
    assert agents["hermes"]["profiles"] == [{"id": "m4", "display_name": "M4 PMA"}]


def test_list_agents_merges_alias_only_hermes_profiles(monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "hermes 0.1.0",
                "launch_mode": "binary",
                "message": "ready",
                "fix": None,
            },
        )(),
    )

    def _fake_options(_ctx):
        return (
            ChatAgentProfileOption(
                agent="hermes",
                profile="m4-pma",
                runtime_agent="hermes-m4-pma",
                description="Hermes M4 PMA",
            ),
        )

    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.agents.chat_hermes_profile_options",
        _fake_options,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.agents.get_available_agents",
        lambda _state: {
            "hermes": SimpleNamespace(name="Hermes", capabilities=set()),
        },
    )

    app = FastAPI()
    app.state.app_server_supervisor = MagicMock()
    app.state.opencode_supervisor = MagicMock()
    app.state.app_server_events = MagicMock()
    app.state.config = SimpleNamespace(
        agent_binary=lambda _agent_id, profile=None: "hermes",
        agent_profiles=lambda agent_id: (
            {"base": SimpleNamespace(display_name="Base")}
            if agent_id == "hermes"
            else {}
        ),
        agent_default_profile=lambda _agent_id: None,
    )
    app.state.engine = SimpleNamespace(repo_root=_TEST_REPO_ROOT)
    app.include_router(build_agents_routes())

    with TestClient(app) as client:
        response = client.get("/api/agents")

    assert response.status_code == 200
    agents = {agent["id"]: agent for agent in response.json()["agents"]}
    profs = {p["id"]: p["display_name"] for p in agents["hermes"]["profiles"]}
    assert profs == {
        "base": "Base",
        "m4-pma": "Hermes M4 PMA",
    }


def test_models_endpoint_returns_capability_error_for_unknown_agent() -> None:
    client = _build_client(with_supervisors=True)

    response = client.get("/api/agents/unknown-agent/models")

    assert response.status_code == 404
    data = response.json()
    assert "Unknown agent" in data["detail"]


def test_events_endpoint_returns_capability_error_for_agent_without_event_streaming(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.zeroclaw_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "zeroclaw 0.2.0",
                "launch_mode": "binary",
                "message": "ready",
                "fix": None,
            },
        )(),
    )
    from codex_autorunner.agents.zeroclaw.harness import ZEROCLAW_CAPABILITIES

    if "event_streaming" in ZEROCLAW_CAPABILITIES:
        return

    client = _build_client(with_supervisors=True)

    response = client.get(
        "/api/agents/zeroclaw/turns/turn-123/events",
        params={"thread_id": "thread-123"},
    )

    assert response.status_code == 400
    data = response.json()
    assert "event_streaming" in data["detail"]


def test_events_endpoint_returns_unknown_agent_for_unregistered() -> None:
    client = _build_client(with_supervisors=True)

    response = client.get(
        "/api/agents/nonexistent-agent/turns/turn-123/events",
        params={"thread_id": "thread-123"},
    )

    assert response.status_code == 404
    data = response.json()
    assert "Unknown agent" in data["detail"]
