from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.state import RunnerState, save_state
from codex_autorunner.surfaces.web.routes.pma_routes.meta import build_pma_meta_routes

_TEST_HUB_ROOT = "/workspace/test-hub"
_TEST_REPO_ROOT = "/workspace/test-repo"


def _build_client(with_supervisors: bool = False) -> TestClient:
    app = FastAPI()
    router = app.router

    if with_supervisors:
        app.state.app_server_supervisor = MagicMock()
        app.state.opencode_supervisor = MagicMock()
        app.state.app_server_events = MagicMock()
        app.state.config = SimpleNamespace(
            root=_TEST_HUB_ROOT,
            raw={"pma": {"enabled": True}},
            agent_binary=lambda _agent_id: "hermes",
        )
        app.state.engine = SimpleNamespace(repo_root=_TEST_REPO_ROOT)

    def get_runtime_state():
        return SimpleNamespace(
            get_safety_checker=lambda _hub_root, _request: MagicMock(
                _audit_log=MagicMock(list_recent=lambda limit: []),
                get_stats=lambda: {},
            )
        )

    build_pma_meta_routes(router, get_runtime_state)
    return TestClient(app)


def test_pma_agents_includes_hermes_when_available(monkeypatch) -> None:
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

    response = client.get("/agents")

    assert response.status_code == 200
    data = response.json()
    agents = {agent["id"]: agent for agent in data.get("agents", [])}

    if "hermes" in agents:
        hermes_caps = agents["hermes"]["capabilities"]
        assert "durable_threads" in hermes_caps
        assert "message_turns" in hermes_caps
        assert "interrupt" in hermes_caps
        assert "event_streaming" in hermes_caps
        assert "model_listing" not in hermes_caps
        assert agents["hermes"]["capability_projection"]["actions"]["list_models"][
            "missing_capabilities"
        ] == ["model_listing"]


def test_pma_models_endpoint_returns_capability_error_for_hermes(monkeypatch) -> None:
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

    response = client.get("/agents/hermes/models")

    assert response.status_code == 400
    data = response.json()
    assert "model_listing" in data["detail"]
    assert "hermes" in data["detail"]


def test_pma_models_endpoint_returns_unknown_agent_for_unregistered() -> None:
    client = _build_client(with_supervisors=True)

    response = client.get("/agents/unknown-agent/models")

    assert response.status_code == 404
    data = response.json()
    assert "Unknown agent" in data["detail"]


def test_pma_agents_includes_profile_defaults(monkeypatch) -> None:
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
    router = app.router
    app.state.app_server_supervisor = MagicMock()
    app.state.opencode_supervisor = MagicMock()
    app.state.app_server_events = MagicMock()
    app.state.config = SimpleNamespace(
        root=_TEST_HUB_ROOT,
        raw={"pma": {"enabled": True, "profile": "m4"}},
        agent_binary=lambda _agent_id, profile=None: (
            "hermes" if profile else "opencode"
        ),
        agent_profiles=lambda agent_id: (
            {"m4": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_default_profile=lambda agent_id: "m4" if agent_id == "hermes" else None,
    )
    app.state.engine = SimpleNamespace(repo_root=_TEST_REPO_ROOT)

    def get_runtime_state():
        return SimpleNamespace(
            get_safety_checker=lambda _hub_root, _request: MagicMock(
                _audit_log=MagicMock(list_recent=lambda limit: []),
                get_stats=lambda: {},
            )
        )

    build_pma_meta_routes(router, get_runtime_state)

    with TestClient(app) as client:
        response = client.get("/agents")

    assert response.status_code == 200
    payload = response.json()
    assert payload["defaults"]["profile"] == "m4"
    agents = {agent["id"]: agent for agent in payload["agents"]}
    assert agents["hermes"]["default_profile"] == "m4"


def test_pma_agents_honors_configured_default_agent(tmp_path) -> None:
    app = FastAPI()
    router = app.router
    state_path = tmp_path / "state.sqlite3"
    save_state(
        state_path,
        RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            autorunner_model_overrides={"opencode": "zai/glm-4.6"},
        ),
    )
    app.state.app_server_supervisor = MagicMock()
    app.state.opencode_supervisor = MagicMock()
    app.state.app_server_events = MagicMock()
    app.state.config = SimpleNamespace(
        root=_TEST_HUB_ROOT,
        raw={"pma": {"enabled": True, "default_agent": "opencode"}},
        agent_binary=lambda _agent_id: "opencode",
    )
    app.state.engine = SimpleNamespace(
        repo_root=_TEST_REPO_ROOT,
        state_path=state_path,
    )

    def get_runtime_state():
        return SimpleNamespace(
            get_safety_checker=lambda _hub_root, _request: MagicMock(
                _audit_log=MagicMock(list_recent=lambda limit: []),
                get_stats=lambda: {},
            )
        )

    build_pma_meta_routes(router, get_runtime_state)

    with TestClient(app) as client:
        response = client.get("/agents")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default"] == "opencode"
    assert payload["defaults"]["agent"] == "opencode"
    assert payload["defaults"]["model"] == "zai/glm-4.6"


def test_pma_agents_does_not_default_to_unavailable_synthetic_hermes(
    monkeypatch, tmp_path
) -> None:
    opencode_descriptor = AgentDescriptor(
        id="opencode",
        name="OpenCode",
        capabilities=frozenset(),
        make_harness=lambda _ctx: object(),
    )
    hermes_descriptor = AgentDescriptor(
        id="hermes",
        name="Hermes",
        capabilities=frozenset(),
        make_harness=lambda _ctx: object(),
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.pma_routes.meta.get_available_agents",
        lambda _context: {"opencode": opencode_descriptor},
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.pma_routes.meta.get_agent_descriptor",
        lambda agent_id, _context: hermes_descriptor if agent_id == "hermes" else None,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.pma_routes.meta._available_agents",
        lambda _request: (
            [
                {
                    "id": "opencode",
                    "name": "OpenCode",
                    "capabilities": [],
                    "capability_projection": {},
                }
            ],
            "opencode",
        ),
    )

    app = FastAPI()
    router = app.router
    state_path = tmp_path / "state.sqlite3"
    save_state(
        state_path,
        RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
        ),
    )
    app.state.app_server_supervisor = MagicMock()
    app.state.opencode_supervisor = MagicMock()
    app.state.app_server_events = MagicMock()
    app.state.config = SimpleNamespace(
        root=_TEST_HUB_ROOT,
        raw={"pma": {"enabled": True, "default_agent": "hermes"}},
        agent_backend=lambda agent_id: "hermes" if agent_id == "hermes" else None,
        agent_binary=lambda _agent_id: "opencode",
    )
    app.state.engine = SimpleNamespace(
        repo_root=_TEST_REPO_ROOT,
        state_path=state_path,
    )

    def get_runtime_state():
        return SimpleNamespace(
            get_safety_checker=lambda _hub_root, _request: MagicMock(
                _audit_log=MagicMock(list_recent=lambda limit: []),
                get_stats=lambda: {},
            )
        )

    build_pma_meta_routes(router, get_runtime_state)

    with TestClient(app) as client:
        response = client.get("/agents")

    assert response.status_code == 200
    payload = response.json()
    agent_ids = {agent["id"] for agent in payload["agents"]}
    assert "hermes" in agent_ids
    assert payload["default"] == "opencode"
    assert payload["defaults"]["agent"] == "opencode"


def test_pma_agents_defaults_include_settings_default_model(tmp_path) -> None:
    app = FastAPI()
    router = app.router
    state_path = tmp_path / "state.sqlite3"
    save_state(
        state_path,
        RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            autorunner_model_overrides={"codex": "gpt-settings-default"},
        ),
    )
    app.state.app_server_supervisor = MagicMock()
    app.state.opencode_supervisor = MagicMock()
    app.state.app_server_events = MagicMock()
    app.state.config = SimpleNamespace(
        root=_TEST_HUB_ROOT,
        raw={"pma": {"enabled": True, "model": "gpt-pma-config-default"}},
        codex_model="gpt-config-default",
        agent_binary=lambda _agent_id: "codex",
    )
    app.state.engine = SimpleNamespace(
        repo_root=_TEST_REPO_ROOT,
        state_path=state_path,
    )

    def get_runtime_state():
        return SimpleNamespace(
            get_safety_checker=lambda _hub_root, _request: MagicMock(
                _audit_log=MagicMock(list_recent=lambda limit: []),
                get_stats=lambda: {},
            )
        )

    build_pma_meta_routes(router, get_runtime_state)

    with TestClient(app) as client:
        response = client.get("/agents")

    assert response.status_code == 200
    assert response.json()["defaults"]["model"] == "gpt-settings-default"
