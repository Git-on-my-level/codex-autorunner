from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadOutcome
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes import managed_thread_runtime

pytestmark = pytest.mark.slow


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def test_managed_thread_message_route_uses_orchestration_service_seam(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
        def __init__(self) -> None:
            self.record_calls: list[dict[str, Any]] = []

        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(
            self,
            thread_target_id: str,
            execution_id: str,
            *,
            status: str,
            assistant_text: Optional[str] = None,
            error: Optional[str] = None,
            backend_turn_id: Optional[str] = None,
            transcript_turn_id: Optional[str] = None,
        ):
            self.record_calls.append(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": execution_id,
                    "status": status,
                    "assistant_text": assistant_text,
                    "backend_turn_id": backend_turn_id,
                    "transcript_turn_id": transcript_turn_id,
                }
            )
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    fake_service = FakeService()

    def _fake_build_service(request, *, thread_store=None):
        _ = request, thread_store
        return fake_service

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        captured["service"] = service
        captured["request"] = request
        captured["sandbox_policy"] = sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        _fake_build_service,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "await_runtime_thread_outcome",
        _fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["assistant_text"] == "assistant-output"
    assert captured["sandbox_policy"] == "dangerFullAccess"
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .endswith("hello from route\n</user_message>\n")
    )
    assert fake_service.record_calls[0]["status"] == "ok"
    assert fake_service.record_calls[0]["execution_id"] == "managed-turn-1"


def test_managed_thread_interrupt_route_uses_orchestration_service_seam(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="interrupt me")
    managed_turn_id = str(turn["managed_turn_id"])
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")
    calls: list[str] = []

    class FakeService:
        async def interrupt_thread(self, thread_target_id: str):
            calls.append(thread_target_id)
            store.mark_turn_interrupted(managed_turn_id)
            return SimpleNamespace(status="interrupted")

        async def stop_thread(self, thread_target_id: str):
            calls.append(thread_target_id)
            store.mark_turn_interrupted(managed_turn_id)
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                execution=SimpleNamespace(status="interrupted"),
                interrupted_active=True,
                recovered_lost_backend=False,
                cancelled_queued=0,
            )

        def record_execution_interrupted(
            self, thread_target_id: str, execution_id: str
        ):
            _ = thread_target_id, execution_id
            store.mark_turn_interrupted(execution_id)
            return SimpleNamespace(status="interrupted")

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return SimpleNamespace(status="interrupted")

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )

    with TestClient(app) as client:
        response = client.post(f"/hub/pma/threads/{managed_thread_id}/interrupt")

    assert response.status_code == 200
    assert calls == [managed_thread_id]


def test_interrupt_fails_for_agent_without_interrupt_capability(
    hub_env,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])

    def mock_get_running_turn(thread_id: str):
        return {"managed_turn_id": "test-turn-id", "status": "running"}

    monkeypatch.setattr(store, "get_running_turn", mock_get_running_turn)

    def mock_get_available_agents(state):
        return {
            "codex": MagicMock(
                capabilities=frozenset(["durable_threads", "message_turns"])
            )
        }

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_available_agents",
        mock_get_available_agents,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.orchestration.catalog.map_agent_capabilities",
        lambda caps: list(caps),
    )

    with TestClient(app) as client:
        response = client.post(f"/hub/pma/threads/{managed_thread_id}/interrupt")

    assert response.status_code == 403
    assert "does not support interrupt" in response.json()["detail"]
    assert "interrupt" in response.json()["detail"]
