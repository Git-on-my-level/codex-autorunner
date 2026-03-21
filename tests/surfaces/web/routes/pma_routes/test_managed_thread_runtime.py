from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadOutcome
from codex_autorunner.core.orchestration.turn_timeline import list_turn_timeline
from codex_autorunner.core.pma_context import format_pma_discoverability_preamble
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
    assert payload["delivered_message"] == "hello from route"
    assert captured["request"].approval_mode == "never"
    assert captured["sandbox_policy"] == "dangerFullAccess"
    assert captured["request"].context_profile == "car_ambient"
    assert "<injected context>" not in captured["request"].metadata["runtime_prompt"]
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .startswith(format_pma_discoverability_preamble(hub_root=hub_env.hub_root))
    )
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .endswith("hello from route\n</user_message>\n")
    )
    assert fake_service.record_calls[0]["status"] == "ok"
    assert fake_service.record_calls[0]["execution_id"] == "managed-turn-1"


def test_managed_thread_message_route_honors_explicit_core_context_profile(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"context_profile": "car_core"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
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
            _ = (
                thread_target_id,
                execution_id,
                assistant_text,
                error,
                backend_turn_id,
                transcript_turn_id,
            )
            return SimpleNamespace(status=status, error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
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
        lambda request, *, thread_store=None: FakeService(),
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
    assert captured["request"].context_profile == "car_core"
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .startswith(format_pma_discoverability_preamble(hub_root=hub_env.hub_root))
    )
    assert "<injected context>" in captured["request"].metadata["runtime_prompt"]


def test_managed_thread_message_route_honors_explicit_approval_override(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"approval_mode": "read-only"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
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
            _ = (
                thread_target_id,
                execution_id,
                assistant_text,
                error,
                backend_turn_id,
                transcript_turn_id,
            )
            return SimpleNamespace(status=status, error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id
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
        lambda request, *, thread_store=None: FakeService(),
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
    assert captured["request"].approval_mode == "on-request"
    assert captured["sandbox_policy"] == "readOnly"


def test_managed_thread_message_route_injects_core_context_when_profile_is_core(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"context_profile": "car_core"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

        def get_running_execution(self, thread_target_id: str):
            _ = thread_target_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
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
        lambda request, *, thread_store=None: FakeService(),
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
    assert captured["request"].context_profile == "car_core"
    assert "<injected context>" in captured["request"].metadata["runtime_prompt"]


def test_managed_thread_message_route_preserves_literal_message_whitespace(
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
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
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
        lambda request, *, thread_store=None: FakeService(),
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

    literal_message = "  keep literal backticks `glm-5-turbo`\nsecond line\n"
    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": literal_message},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["delivered_message"] == literal_message
    assert captured["request"].message_text == literal_message
    runtime_prompt = captured["request"].metadata["runtime_prompt"]
    assert f"<user_message>\n{literal_message}" in runtime_prompt
    assert runtime_prompt.endswith("\n</user_message>\n")


def test_managed_thread_message_persists_full_timeline_from_raw_events(
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
    raw_events = (
        {
            "message": {
                "method": "item/toolCall/start",
                "params": {
                    "item": {"toolCall": {"name": "shell", "input": {"cmd": "pwd"}}}
                },
            }
        },
        {
            "message": {
                "method": "item/toolCall/end",
                "params": {
                    "name": "shell",
                    "result": {"stdout": str(hub_env.repo_root)},
                },
            }
        },
    )
    stream_started = False

    class FakeHarness:
        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            nonlocal stream_started
            _ = workspace_root, conversation_id, turn_id
            yield raw_events[0]
            stream_started = True
            await asyncio.Future()

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None, backend_id="backend-turn-1")

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

        def get_running_execution(self, thread_target_id: str):
            _ = thread_target_id
            return None

        def claim_next_queued_execution_request(self, thread_target_id: str):
            _ = thread_target_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-raw-events",
                backend_id="backend-turn-1",
                status="running",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
            harness=FakeHarness(),
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        while not stream_started:
            await asyncio.sleep(0)
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
            raw_events=(raw_events[1],),
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
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
    timeline = list_turn_timeline(
        hub_env.hub_root, execution_id="managed-turn-raw-events"
    )
    assert [entry["event_type"] for entry in timeline] == [
        "tool_call",
        "tool_result",
        "turn_completed",
    ]


def test_zeroclaw_managed_thread_projects_compat_agents_file_for_core_profile(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    workspace_root = (
        hub_env.hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "zeroclaw",
        workspace_root.resolve(),
        resource_kind="agent_workspace",
        resource_id="zc-main",
        metadata={"context_profile": "car_core"},
    )
    managed_thread_id = str(created["managed_thread_id"])

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=workspace_root.resolve(),
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
        lambda request, *, thread_store=None: FakeService(),
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
            json={"message": "sync runtime context"},
        )

    assert response.status_code == 200
    agents_path = workspace_root / "workspace" / "AGENTS.md"
    assert agents_path.exists()
    assert "# AGENTS" in agents_path.read_text(encoding="utf-8")


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
