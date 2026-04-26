import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration import (
    ColdTraceStore,
    ExecutionRecord,
    ThreadTarget,
)
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerResponseError,
)
from codex_autorunner.integrations.app_server.threads import PMA_KEY, PMA_OPENCODE_KEY
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore, topic_key
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import pma as pma_routes
from codex_autorunner.surfaces.web.routes.pma_routes import (
    chat_runtime,
    tail_stream,
)
from codex_autorunner.surfaces.web.routes.pma_routes import publish as publish_routes
from tests.conftest import write_test_config
from tests.pma_support import (
    _disable_pma,
    _enable_pma,
    _install_fake_successful_chat_supervisor,
    _seed_discord_pma_binding,
    _seed_telegram_pma_binding,
)

pytestmark = pytest.mark.slow


def test_build_pma_routes_does_not_construct_async_primitives_on_route_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _lock_ctor() -> object:
        raise AssertionError("async primitives must be lazily initialized")

    monkeypatch.setattr(pma_routes.asyncio, "Lock", _lock_ctor)
    pma_routes.build_pma_routes()


def test_build_pma_routes_registers_unique_method_path_pairs() -> None:
    router = pma_routes.build_pma_routes()
    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()

    for route in router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or ()
        if not isinstance(path, str):
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            pair = (str(method), path)
            if pair in seen:
                duplicates.add(pair)
            seen.add(pair)

    assert duplicates == set()


@pytest.mark.parametrize(
    ("method", "endpoint", "body"),
    [
        ("GET", "/hub/pma/targets", None),
        ("GET", "/hub/pma/targets/active", None),
        ("POST", "/hub/pma/targets/active", {"key": "chat:telegram:100"}),
        ("POST", "/hub/pma/targets/add", {"ref": "web"}),
        ("POST", "/hub/pma/targets/remove", {"key": "web"}),
        ("POST", "/hub/pma/targets/clear", {}),
    ],
)
def test_pma_target_endpoints_are_removed(
    hub_env, method: str, endpoint: str, body: Optional[dict[str, Any]]
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    if method == "GET":
        resp = client.get(endpoint)
    else:
        resp = client.post(endpoint, json=body)
    assert resp.status_code == 404


def test_pma_agents_endpoint(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.get("/hub/pma/agents")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("agents"), list)
    assert payload.get("default") in {agent.get("id") for agent in payload["agents"]}


def test_pma_agents_endpoint_advertises_hermes_active_thread_discovery(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
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
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/agents")

    assert resp.status_code == 200
    payload = resp.json()
    agents = {agent["id"]: agent for agent in payload["agents"]}
    assert "hermes" in agents
    assert "active_thread_discovery" in agents["hermes"]["capabilities"]


def test_pma_agents_endpoint_surfaces_hermes_optional_controls_and_commands(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class _HermesSupervisor:
        async def session_capabilities(self, workspace_root: Path):
            _ = workspace_root
            return type(
                "Caps",
                (),
                {
                    "list_sessions": True,
                    "fork": True,
                    "set_model": True,
                    "set_mode": False,
                },
            )()

        async def advertised_commands(self, workspace_root: Path):
            _ = workspace_root
            return [
                type(
                    "Command",
                    (),
                    {"name": "/fork", "description": "Fork the current session"},
                )(),
                type(
                    "Command",
                    (),
                    {"name": "/model", "description": "Switch model"},
                )(),
            ]

    app.state.hermes_supervisor = _HermesSupervisor()
    with TestClient(app) as client:
        resp = client.get("/hub/pma/agents")

    assert resp.status_code == 200
    payload = resp.json()
    agents = {agent["id"]: agent for agent in payload["agents"]}
    hermes = agents["hermes"]
    assert hermes["session_controls"] == {
        "fork": True,
        "set_model": True,
        "set_mode": False,
        "list_sessions": True,
    }
    assert hermes["advertised_commands"] == [
        {"name": "/fork", "description": "Fork the current session"},
        {"name": "/model", "description": "Switch model"},
    ]


def test_pma_agents_endpoint_prefers_hermes_default_profile_over_global_default(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    cfg["pma"]["profile"] = "codex-profile"
    cfg.setdefault("agents", {})
    cfg["agents"]["hermes"] = {
        "binary": "hermes",
        "profiles": {"m4": {"binary": "hermes-m4"}},
        "default_profile": "m4",
    }
    write_test_config(hub_env.hub_root / CONFIG_FILENAME, cfg)
    app = create_hub_app(hub_env.hub_root)
    observed: dict[str, Any] = {"profiles": []}

    class _HermesSupervisor:
        async def session_capabilities(self, workspace_root: Path):
            _ = workspace_root
            return type(
                "Caps",
                (),
                {
                    "list_sessions": True,
                    "fork": True,
                    "set_model": False,
                    "set_mode": False,
                },
            )()

        async def advertised_commands(self, workspace_root: Path):
            _ = workspace_root
            return []

    def _build_supervisor(_config, *, profile=None, **_kwargs):
        observed["profiles"].append(profile)
        return _HermesSupervisor()

    with TestClient(app) as client:
        app.state.pma_container.ports.build_hermes_supervisor = _build_supervisor
        first = client.get("/hub/pma/agents")
        second = client.get("/hub/pma/agents")

    assert first.status_code == 200
    assert second.status_code == 200
    assert observed["profiles"] == ["m4"]
    hermes = {agent["id"]: agent for agent in first.json()["agents"]}["hermes"]
    assert hermes["metadata_profile"] == "m4"


def test_pma_agents_endpoint_includes_unavailable_hermes_static_profile_metadata(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    cfg.setdefault("agents", {})
    cfg["agents"]["hermes"] = {
        "binary": "hermes",
        "profiles": {"m4": {"binary": "hermes-m4", "display_name": "M4 PMA"}},
        "default_profile": "m4",
    }
    write_test_config(hub_env.hub_root / CONFIG_FILENAME, cfg)

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "missing",
                "version": None,
                "launch_mode": "binary",
                "message": "binary missing",
                "fix": "install hermes",
            },
        )(),
    )
    app = create_hub_app(hub_env.hub_root)
    app.state.pma_container.ports.build_hermes_supervisor = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(AssertionError("unavailable hermes should not resolve supervisor metadata"))

    with TestClient(app) as client:
        resp = client.get("/hub/pma/agents")

    assert resp.status_code == 200
    payload = resp.json()
    agents = {agent["id"]: agent for agent in payload["agents"]}
    hermes = agents["hermes"]
    assert hermes["default_profile"] == "m4"
    assert hermes["profiles"] == [{"id": "m4", "display_name": "M4 PMA"}]
    assert hermes["metadata_profile"] == "m4"
    assert "session_controls" not in hermes
    assert "advertised_commands" not in hermes


def test_pma_chat_requires_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={})
    assert resp.status_code == 400


def test_pma_chat_submits_through_surface_orchestration_ingress(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    captured: dict[str, Any] = {}

    class _FakeIngress:
        async def submit_message(
            self,
            request,
            *,
            resolve_paused_flow_target,
            submit_flow_reply,
            submit_thread_message,
        ):
            _ = resolve_paused_flow_target, submit_flow_reply, submit_thread_message
            captured["surface_kind"] = request.surface_kind
            captured["prompt_text"] = request.prompt_text
            captured["pma_enabled"] = request.pma_enabled
            return SimpleNamespace(
                route="thread",
                thread_result={"status": "ok", "message": "ingress ok"},
            )

    monkeypatch.setattr(
        chat_runtime,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    app.state.app_server_supervisor = object()
    app.state.app_server_events = object()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hello through ingress"})

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "message": "ingress ok",
        "client_turn_id": "",
    }
    assert captured == {
        "surface_kind": "web",
        "prompt_text": "hello through ingress",
        "pma_enabled": True,
    }


@pytest.mark.anyio
async def test_pma_chat_returns_completed_queue_result_when_future_is_registered_late(
    hub_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    queue = PmaQueue(hub_env.hub_root)
    original_enqueue = queue.enqueue

    async def _enqueue_and_complete(
        lane_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ):
        item, dupe_reason = await original_enqueue(lane_id, idempotency_key, payload)
        await queue.complete_item(
            item,
            {
                "status": "ok",
                "message": "late queue result",
                "client_turn_id": "",
            },
        )
        return item, dupe_reason

    async def _ensure_lane_worker(*args: Any, **kwargs: Any) -> None:
        _ = args, kwargs

    monkeypatch.setattr(queue, "enqueue", _enqueue_and_complete)
    monkeypatch.setattr(
        chat_runtime.PmaRuntimeState,
        "get_pma_queue",
        lambda self, hub_root: queue,
    )
    monkeypatch.setattr(
        chat_runtime.PmaRuntimeState,
        "ensure_lane_worker",
        _ensure_lane_worker,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post("/hub/pma/chat", json={"message": "hello"})

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "message": "late queue result",
        "client_turn_id": "",
    }


def test_pma_thread_status_includes_queued_turns(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    running_turn = store.create_turn(managed_thread_id, prompt="first")
    queued_turn = store.create_turn(
        managed_thread_id,
        prompt="second",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second"}},
    )

    client = TestClient(app)
    resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["turn"]["managed_turn_id"] == running_turn["managed_turn_id"]
    assert payload["queue_depth"] == 1
    assert len(payload["queued_turns"]) == 1
    assert (
        payload["queued_turns"][0]["managed_turn_id"] == queued_turn["managed_turn_id"]
    )
    assert payload["queued_turns"][0]["request_kind"] == "message"
    assert payload["queued_turns"][0]["state"] == "queued"
    assert payload["queued_turns"][0]["position"] == 1
    assert payload["queued_turns"][0]["prompt_preview"] == "second"


def test_pma_thread_queue_route_lists_pending_turns(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    store.create_turn(managed_thread_id, prompt="first")
    first_queued_turn = store.create_turn(
        managed_thread_id,
        prompt="second",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second"}},
    )
    second_queued_turn = store.create_turn(
        managed_thread_id,
        prompt="third",
        request_kind="review",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "third", "kind": "review"}},
    )
    queued_items = store.list_pending_turn_queue_items(managed_thread_id)

    client = TestClient(app)
    resp = client.get(f"/hub/pma/threads/{managed_thread_id}/queue")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["managed_thread_id"] == managed_thread_id
    assert payload["queue_depth"] == 2
    assert payload["queued_turns"] == [
        {
            "managed_turn_id": first_queued_turn["managed_turn_id"],
            "request_kind": "message",
            "state": "queued",
            "position": 1,
            "enqueued_at": queued_items[0]["enqueued_at"],
            "prompt_preview": "second",
            "model": None,
            "reasoning": None,
            "client_turn_id": None,
        },
        {
            "managed_turn_id": second_queued_turn["managed_turn_id"],
            "request_kind": "review",
            "state": "queued",
            "position": 2,
            "enqueued_at": queued_items[1]["enqueued_at"],
            "prompt_preview": "third",
            "model": None,
            "reasoning": None,
            "client_turn_id": None,
        },
    ]


def test_pma_thread_queue_cancel_route_cancels_specific_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    store.create_turn(managed_thread_id, prompt="first")
    queued_turn = store.create_turn(
        managed_thread_id,
        prompt="second",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "second"}},
    )
    trailing_turn = store.create_turn(
        managed_thread_id,
        prompt="third",
        busy_policy="queue",
        queue_payload={"request": {"message_text": "third"}},
    )

    client = TestClient(app)
    resp = client.post(
        f"/hub/pma/threads/{managed_thread_id}/queue/{queued_turn['managed_turn_id']}/cancel"
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload == {
        "status": "ok",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": queued_turn["managed_turn_id"],
        "cancelled": True,
        "position": 1,
        "queue_depth": 1,
    }

    cancelled_turn = store.get_turn(managed_thread_id, queued_turn["managed_turn_id"])
    assert cancelled_turn is not None
    assert cancelled_turn["status"] == "interrupted"
    remaining = store.list_pending_turn_queue_items(managed_thread_id)
    assert [item["managed_turn_id"] for item in remaining] == [
        trailing_turn["managed_turn_id"]
    ]


def test_pma_chat_rejects_oversize_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root, max_text_chars=5)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "toolong"})
    assert resp.status_code == 400
    payload = resp.json()
    assert "max_text_chars" in (payload.get("detail") or "")


def test_pma_routes_enabled_by_default(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code == 200
    assert client.post("/hub/pma/chat", json={}).status_code == 400


def test_pma_routes_disabled_by_config(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code == 404
    assert client.post("/hub/pma/chat", json={"message": "hi"}).status_code == 404


def test_pma_chat_applies_model_reasoning_defaults(hub_env) -> None:
    _enable_pma(hub_env.hub_root, model="test-model", reasoning="high")

    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_kwargs = None

        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            self.turn_kwargs = turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert app.state.app_server_supervisor.client.turn_kwargs == {
        "model": "test-model",
        "effort": "high",
    }


def test_pma_chat_response_omits_legacy_delivery_fields(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(app, turn_id="turn-clean-response")

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert "delivery_outcome" not in payload
    assert "dispatch_delivery_outcome" not in payload
    assert "delivery_status" not in payload


def test_pma_chat_register_turn_failure_is_best_effort(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(app, turn_id="turn-register-failure")

    class _BrokenEvents:
        async def register_turn(self, thread_id: str, turn_id: str) -> None:
            _ = thread_id, turn_id
            raise RuntimeError("buffer unavailable")

    app.state.app_server_events = _BrokenEvents()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_execute_opencode_records_completion_only_messages_in_timeline(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Client:
        async def create_session(
            self, directory: Optional[str] = None
        ) -> dict[str, str]:
            _ = directory
            return {"sessionId": "session-1"}

        async def prompt_async(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "info": {"id": "message-1", "role": "assistant"},
                "parts": [{"type": "text", "text": "completed only reply"}],
            }

    class _Supervisor:
        async def get_client(self, _hub_root: Path) -> _Client:
            return _Client()

        async def mark_turn_started(self, _hub_root: Path) -> None:
            return None

        async def mark_turn_finished(self, _hub_root: Path) -> None:
            return None

    async def _fake_collect(*_args: Any, **kwargs: Any) -> OpenCodeTurnOutput:
        ready_event = kwargs.get("ready_event")
        if ready_event is not None:
            ready_event.set()
        return OpenCodeTurnOutput(text="completed only reply")

    monkeypatch.setattr(
        "codex_autorunner.agents.opencode.runtime.collect_opencode_output",
        _fake_collect,
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.opencode.runtime.build_turn_id",
        lambda session_id: f"{session_id}:turn",
    )

    result = await chat_runtime._execute_opencode(
        _Supervisor(),
        hub_env.hub_root,
        "hello",
        asyncio.Event(),
    )

    assert [type(event).__name__ for event in result["timeline_events"]] == [
        "OutputDelta",
        "Completed",
    ]
    assert result["timeline_events"][0].content == "completed only reply"


def test_pma_chat_persists_transcript_and_history_entry(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-transcript",
        message="assistant transcript payload",
    )

    client = TestClient(app)
    resp = client.post(
        "/hub/pma/chat",
        json={"message": "persist transcript", "client_turn_id": "client-transcript"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"

    active_payload = client.get("/hub/pma/active").json()
    last_result = active_payload["last_result"]
    transcript_pointer = last_result.get("transcript") or {}
    transcript_turn_id = str(transcript_pointer.get("turn_id") or "")
    assert transcript_turn_id

    transcript = PmaTranscriptStore(hub_env.hub_root).read_transcript(
        transcript_turn_id
    )
    assert transcript is not None
    assert transcript["content"].strip() == (
        "User:\npersist transcript\n\nAssistant:\nassistant transcript payload"
    )
    metadata = transcript["metadata"]
    assert metadata["client_turn_id"] == "client-transcript"
    assert metadata["trigger"] == "user_prompt"
    assert metadata["lane_id"] == "pma:default"

    history_payload = client.get("/hub/pma/history").json()
    assert any(
        entry.get("turn_id") == transcript_turn_id
        for entry in history_payload.get("entries", [])
    )

    history_entry = client.get(f"/hub/pma/history/{transcript_turn_id}")
    assert history_entry.status_code == 200
    assert history_entry.json()["content"].strip() == (
        "User:\npersist transcript\n\nAssistant:\nassistant transcript payload"
    )


def test_pma_history_detail_includes_turn_timeline(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-timeline",
        message="assistant transcript payload",
        raw_events=[
            {
                "message": {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": "reason-1", "delta": "inspect state"},
                }
            },
            {
                "message": {
                    "method": "item/toolCall/start",
                    "params": {
                        "item": {
                            "toolCall": {
                                "name": "shell",
                                "input": {"cmd": "pwd"},
                            }
                        }
                    },
                }
            },
            {
                "message": {
                    "method": "item/toolCall/end",
                    "params": {
                        "name": "shell",
                        "result": {"stdout": "/tmp"},
                    },
                }
            },
        ],
    )

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "persist transcript"})
    assert resp.status_code == 200

    history_entry = client.get("/hub/pma/history/turn-timeline")
    assert history_entry.status_code == 200
    payload = history_entry.json()
    assert [item["event_type"] for item in payload["timeline"]] == [
        "run_notice",
        "tool_call",
        "tool_result",
        "turn_completed",
    ]
    assert payload["timeline"][0]["event"]["message"] == "inspect state"
    assert payload["timeline"][1]["event"]["tool_input"] == {"cmd": "pwd"}
    assert payload["timeline"][2]["event"]["result"] == {"stdout": "/tmp"}
    checkpoint = ColdTraceStore(hub_env.hub_root).load_checkpoint("turn-timeline")
    assert checkpoint is not None
    assert checkpoint.trace_manifest_id


def test_pma_chat_github_injection_uses_raw_user_message(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    observed: dict[str, str] = {}

    async def _fake_github_context_injection(**kwargs):
        observed["link_source_text"] = str(kwargs.get("link_source_text") or "")
        prompt_text = str(kwargs.get("prompt_text") or "")
        return f"{prompt_text}\n\n[injected-from-github]", True

    app = create_hub_app(hub_env.hub_root)
    app.state.pma_container.ports.maybe_inject_github_context = (
        _fake_github_context_injection
    )

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.prompt = None

        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, approval_policy, sandbox_policy, turn_kwargs
            self.prompt = prompt
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    message = "https://github.com/example/repo/issues/321"
    resp = client.post("/hub/pma/chat", json={"message": message})
    assert resp.status_code == 200
    assert observed["link_source_text"] == message
    assert "[injected-from-github]" in str(
        app.state.app_server_supervisor.client.prompt
    )


@pytest.mark.anyio
async def test_pma_chat_idempotency_key_uses_full_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        prefix = "a" * 100
        message_one = f"{prefix}one"
        message_two = f"{prefix}two"
        task_one = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": message_one})
        )
        await anyio.sleep(0.05)
        task_two = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": message_two})
        )
        await anyio.sleep(0.05)
        assert not task_two.done()
        blocker.set()
        with anyio.fail_after(5):
            resp_one = await task_one
            resp_two = await task_two

    assert resp_one.status_code == 200
    assert resp_two.status_code == 200
    assert resp_two.json().get("deduped") is not True


@pytest.mark.anyio
async def test_pma_interrupt_route_interrupts_running_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-interrupt"

        async def wait(self, timeout=None):
            _ = timeout
            await asyncio.Future()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_interrupt_calls: list[tuple[str, Optional[str]]] = []

        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict[str, str]:
            _ = root
            return {"id": "thread-interrupt"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

        async def turn_interrupt(
            self, turn_id: str, *, thread_id: Optional[str] = None
        ) -> None:
            self.turn_interrupt_calls.append((turn_id, thread_id))

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    fake_supervisor = FakeSupervisor()
    app.state.app_server_supervisor = fake_supervisor
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post(
                "/hub/pma/chat",
                json={"message": "interrupt me", "client_turn_id": "turn-interrupt"},
            )
        )
        with anyio.fail_after(2):
            while True:
                active_resp = await client.get("/hub/pma/active")
                payload = active_resp.json()
                current = payload.get("current") or {}
                if (
                    payload.get("active")
                    and current.get("thread_id")
                    and current.get("turn_id")
                ):
                    break
                await anyio.sleep(0.05)

        interrupt_resp = await client.post("/hub/pma/interrupt")
        assert interrupt_resp.status_code == 200
        assert interrupt_resp.json()["interrupted"] is True

        with anyio.fail_after(5):
            chat_resp = await chat_task

        assert chat_resp.status_code == 200
        assert chat_resp.json()["status"] == "interrupted"
        assert chat_resp.json()["detail"] == "PMA chat interrupted"

        with anyio.fail_after(2):
            while True:
                final_active = (await client.get("/hub/pma/active")).json()
                if final_active["last_result"].get("status") == "interrupted":
                    break
                await anyio.sleep(0.05)
        assert final_active["last_result"]["status"] == "interrupted"
        assert final_active["last_result"]["client_turn_id"] == "turn-interrupt"

    assert fake_supervisor.client.turn_interrupt_calls
    assert set(fake_supervisor.client.turn_interrupt_calls) == {
        ("turn-interrupt", "thread-interrupt")
    }


@pytest.mark.anyio
async def test_pma_interrupt_route_interrupts_running_hermes_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    interrupted = asyncio.Event()

    class _HermesSupervisor:
        def __init__(self) -> None:
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def create_session(
            self, workspace_root: Path, title: Optional[str] = None
        ):
            _ = workspace_root, title
            return type("Session", (), {"session_id": "hermes-session-1"})()

        async def resume_session(self, workspace_root: Path, conversation_id: str):
            _ = workspace_root
            return type("Session", (), {"session_id": conversation_id})()

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str] = None,
            approval_mode: Optional[str] = None,
        ) -> str:
            _ = workspace_root, conversation_id, prompt, model, approval_mode
            return "hermes-turn-1"

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
            timeout: Optional[float] = None,
        ):
            _ = workspace_root, conversation_id, turn_id, timeout
            await interrupted.wait()
            return type(
                "Result",
                (),
                {
                    "status": "cancelled",
                    "assistant_text": "",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def interrupt_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            interrupted.set()

        async def stream_turn_events(self, *_args: Any, **_kwargs: Any):
            if False:
                yield {}

    supervisor = _HermesSupervisor()
    app.state.hermes_supervisor = supervisor

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post(
                "/hub/pma/chat",
                json={"message": "interrupt hermes", "agent": "hermes"},
            )
        )
        with anyio.fail_after(2):
            while True:
                active_resp = await client.get("/hub/pma/active")
                payload = active_resp.json()
                current = payload.get("current") or {}
                if (
                    payload.get("active")
                    and current.get("thread_id") == "hermes-session-1"
                    and current.get("turn_id") == "hermes-turn-1"
                ):
                    break
                await anyio.sleep(0.05)

        interrupt_resp = await client.post("/hub/pma/interrupt")
        assert interrupt_resp.status_code == 200
        assert interrupt_resp.json()["agent"] == "hermes"

        with anyio.fail_after(5):
            chat_resp = await chat_task

    assert chat_resp.status_code == 200
    assert chat_resp.json()["status"] == "interrupted"
    assert supervisor.interrupt_calls
    assert set(supervisor.interrupt_calls) == {
        (hub_env.hub_root, "hermes-session-1", "hermes-turn-1")
    }


@pytest.mark.anyio
async def test_pma_active_updates_during_running_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": "hi"})
        )
        try:
            with anyio.fail_after(2):
                while True:
                    resp = await client.get("/hub/pma/active")
                    assert resp.status_code == 200
                    payload = resp.json()
                    current = payload.get("current") or {}
                    if (
                        payload.get("active")
                        and current.get("thread_id")
                        and current.get("turn_id")
                    ):
                        break
                    await anyio.sleep(0.05)
            assert payload["active"] is True
            assert payload["current"]["lane_id"] == "pma:default"
        finally:
            blocker.set()
        resp = await chat_task
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


@pytest.mark.anyio
async def test_pma_second_lane_item_does_not_clobber_active_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()
    turn_start_calls = 0

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            _ = timeout
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            nonlocal turn_start_calls
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            turn_start_calls += 1
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-concurrency"
    start_lane_worker = app.state.pma_lane_worker_start
    stop_lane_worker = app.state.pma_lane_worker_stop
    assert callable(start_lane_worker)
    assert callable(stop_lane_worker)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post(
                "/hub/pma/chat",
                json={"message": "first turn", "client_turn_id": "turn-1"},
            )
        )
        try:
            with anyio.fail_after(2):
                while True:
                    active_resp = await client.get("/hub/pma/active")
                    assert active_resp.status_code == 200
                    active_payload = active_resp.json()
                    current = active_payload.get("current") or {}
                    if (
                        active_payload.get("active")
                        and current.get("thread_id")
                        and current.get("turn_id")
                    ):
                        break
                    await anyio.sleep(0.05)

            first_current = dict(active_payload["current"])
            assert first_current.get("client_turn_id") == "turn-1"
            assert turn_start_calls == 1

            second_item, _ = await queue.enqueue(
                lane_id,
                "pma:test-concurrency:key-2",
                {
                    "message": "second turn",
                    "agent": "codex",
                    "client_turn_id": "turn-2",
                },
            )
            await start_lane_worker(app, lane_id)

            second_result = None
            with anyio.fail_after(2):
                while True:
                    items = await queue.list_items(lane_id)
                    match = next(
                        (
                            entry
                            for entry in items
                            if entry.item_id == second_item.item_id
                        ),
                        None,
                    )
                    assert match is not None
                    if match.state in (
                        QueueItemState.COMPLETED,
                        QueueItemState.FAILED,
                    ):
                        second_result = dict(match.result or {})
                        break
                    await anyio.sleep(0.05)

            assert second_result is not None
            assert second_result.get("status") == "error"
            assert "already active" in (second_result.get("detail") or "").lower()
            assert turn_start_calls == 1

            still_active = (await client.get("/hub/pma/active")).json()
            assert still_active["active"] is True
            assert still_active["current"]["client_turn_id"] == "turn-1"
            assert still_active["current"]["thread_id"] == first_current["thread_id"]
            assert still_active["current"]["turn_id"] == first_current["turn_id"]
        finally:
            await stop_lane_worker(app, lane_id)
            blocker.set()

        first_resp = await chat_task
        assert first_resp.status_code == 200
        assert first_resp.json().get("status") == "ok"


@pytest.mark.anyio
async def test_pma_queue_endpoints_report_lane_items(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-queue"

    item, dupe_reason = await queue.enqueue(
        lane_id,
        "pma:test-queue:key-1",
        {"message": "queued turn", "agent": "codex"},
    )
    assert dupe_reason is None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        summary_resp = await client.get("/hub/pma/queue")
        lane_resp = await client.get(f"/hub/pma/queue/{lane_id}")

    assert summary_resp.status_code == 200
    summary_payload = summary_resp.json()
    assert summary_payload["total_lanes"] >= 1
    assert summary_payload["lanes"][lane_id]["total_items"] == 1
    assert summary_payload["lanes"][lane_id]["by_state"]["pending"] == 1

    assert lane_resp.status_code == 200
    lane_payload = lane_resp.json()
    assert lane_payload["lane_id"] == lane_id
    assert lane_payload["items"] == [
        {
            "item_id": item.item_id,
            "state": "pending",
            "enqueued_at": item.enqueued_at,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "dedupe_reason": None,
        }
    ]


@pytest.mark.anyio
async def test_pma_wakeup_turn_publishes_to_discord_and_telegram_outboxes(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-wakeup-success",
        message="automation summary complete",
    )
    app.state.app_server_events = object()

    await _seed_discord_pma_binding(hub_env, channel_id="discord-123")
    await _seed_telegram_pma_binding(hub_env, chat_id=1001, thread_id=2002)

    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-publish"
    start_lane_worker = app.state.pma_lane_worker_start
    stop_lane_worker = app.state.pma_lane_worker_stop
    assert callable(start_lane_worker)
    assert callable(stop_lane_worker)

    item, _ = await queue.enqueue(
        lane_id,
        "pma:test-publish:key-1",
        {
            "message": "Automation wake-up received.",
            "agent": "codex",
            "client_turn_id": "wakeup-123",
            "wake_up": {
                "wakeup_id": "wakeup-123",
                "repo_id": hub_env.repo_id,
                "event_type": "managed_thread_completed",
                "source": "lifecycle_subscription",
                "run_id": "run-123",
            },
        },
    )

    try:
        await start_lane_worker(app, lane_id)
        result: dict[str, Any] | None = None
        with anyio.fail_after(3):
            while True:
                items = await queue.list_items(lane_id)
                match = next(
                    (entry for entry in items if entry.item_id == item.item_id), None
                )
                assert match is not None
                if match.state in (QueueItemState.COMPLETED, QueueItemState.FAILED):
                    result = dict(match.result or {})
                    break
                await anyio.sleep(0.05)
        assert result is not None
        assert result.get("status") == "ok"
        assert result.get("delivery_status") == "success"
    finally:
        await stop_lane_worker(app, lane_id)

    discord_store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        discord_outbox = await discord_store.list_outbox()
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await discord_store.close()
        await telegram_store.close()

    assert any(record.channel_id == "discord-123" for record in discord_outbox)
    assert not any(
        record.chat_id == 1001 and record.thread_id == 2002
        for record in telegram_outbox
    )


@pytest.mark.anyio
async def test_pma_wakeup_failure_publishes_failure_summary(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    app.state.app_server_supervisor = None
    app.state.app_server_events = None

    await _seed_telegram_pma_binding(hub_env, chat_id=3003, thread_id=4004)

    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-publish-failure"
    start_lane_worker = app.state.pma_lane_worker_start
    stop_lane_worker = app.state.pma_lane_worker_stop
    assert callable(start_lane_worker)
    assert callable(stop_lane_worker)

    item, _ = await queue.enqueue(
        lane_id,
        "pma:test-publish:key-2",
        {
            "message": "Automation wake-up received.",
            "agent": "codex",
            "client_turn_id": "wakeup-456",
            "wake_up": {
                "wakeup_id": "wakeup-456",
                "repo_id": hub_env.repo_id,
                "event_type": "managed_thread_failed",
                "source": "lifecycle_subscription",
                "run_id": "run-456",
            },
        },
    )

    try:
        await start_lane_worker(app, lane_id)
        result: dict[str, Any] | None = None
        with anyio.fail_after(3):
            while True:
                items = await queue.list_items(lane_id)
                match = next(
                    (entry for entry in items if entry.item_id == item.item_id), None
                )
                assert match is not None
                if match.state in (QueueItemState.COMPLETED, QueueItemState.FAILED):
                    result = dict(match.result or {})
                    break
                await anyio.sleep(0.05)
        assert result is not None
        assert result.get("status") == "error"
        assert result.get("delivery_status") == "success"
    finally:
        await stop_lane_worker(app, lane_id)

    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await telegram_store.close()

    failure_message = next(
        (record.text for record in telegram_outbox if record.chat_id == 3003),
        "",
    )
    assert "status: error" in failure_message
    assert "next_action:" in failure_message


@pytest.mark.anyio
async def test_pma_wakeup_publish_retries_transient_telegram_enqueue_failure(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-wakeup-retry",
        message="automation summary complete",
    )
    app.state.app_server_events = object()

    await _seed_telegram_pma_binding(hub_env, chat_id=5005, thread_id=6006)

    original_enqueue_outbox = TelegramStateStore.enqueue_outbox
    original_sleep = publish_routes.asyncio.sleep
    enqueue_attempts = 0
    sleep_calls: list[float] = []

    async def _flaky_enqueue_outbox(self, record):
        nonlocal enqueue_attempts
        enqueue_attempts += 1
        if enqueue_attempts == 1:
            raise RuntimeError("transient-telegram-outbox")
        return await original_enqueue_outbox(self, record)

    async def _fake_sleep(delay: float) -> None:
        if delay in {0.25, 0.75}:
            sleep_calls.append(delay)
            await original_sleep(0)
            return
        await original_sleep(delay)

    monkeypatch.setattr(TelegramStateStore, "enqueue_outbox", _flaky_enqueue_outbox)
    monkeypatch.setattr(publish_routes.asyncio, "sleep", _fake_sleep)

    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-publish-retry"
    start_lane_worker = app.state.pma_lane_worker_start
    stop_lane_worker = app.state.pma_lane_worker_stop
    assert callable(start_lane_worker)
    assert callable(stop_lane_worker)

    item, _ = await queue.enqueue(
        lane_id,
        "pma:test-publish-retry:key-1",
        {
            "message": "Automation wake-up received.",
            "agent": "codex",
            "client_turn_id": "wakeup-retry-123",
            "wake_up": {
                "wakeup_id": "wakeup-retry-123",
                "repo_id": hub_env.repo_id,
                "event_type": "managed_thread_completed",
                "source": "lifecycle_subscription",
                "run_id": "run-retry-123",
            },
        },
    )

    try:
        await start_lane_worker(app, lane_id)
        result: dict[str, Any] | None = None
        with anyio.fail_after(3):
            while True:
                items = await queue.list_items(lane_id)
                match = next(
                    (entry for entry in items if entry.item_id == item.item_id), None
                )
                assert match is not None
                if match.state in (QueueItemState.COMPLETED, QueueItemState.FAILED):
                    result = dict(match.result or {})
                    break
                await anyio.sleep(0.05)
        assert result is not None
        assert result.get("status") == "ok"
        assert result.get("delivery_status") == "success"
        outcome = result.get("delivery_outcome") or {}
        assert outcome.get("published") == 1
        assert outcome.get("targets") == 1
    finally:
        await stop_lane_worker(app, lane_id)

    assert enqueue_attempts == 2
    assert sleep_calls
    assert sleep_calls[0] == 0.25

    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await telegram_store.close()

    matching = [
        record
        for record in telegram_outbox
        if record.chat_id == 5005 and record.thread_id == 6006
    ]
    assert len(matching) == 1
    assert "automation summary complete" in matching[0].text


@pytest.mark.anyio
async def test_pma_wakeup_turn_publishes_using_prev_workspace_repo_context(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-wakeup-prev-workspace",
        message="automation summary complete",
    )
    app.state.app_server_events = object()

    unrelated_workspace = hub_env.hub_root / "unrelated-pma-workspace"
    unrelated_workspace.mkdir(parents=True, exist_ok=True)

    discord_store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        await discord_store.upsert_binding(
            channel_id="discord-prev-workspace",
            guild_id="guild-1",
            workspace_path=str(unrelated_workspace.resolve()),
            repo_id=None,
        )
        await discord_store.update_pma_state(
            channel_id="discord-prev-workspace",
            pma_enabled=True,
            pma_prev_workspace_path=str(hub_env.repo_root.resolve()),
            pma_prev_repo_id=None,
        )

        telegram_key = topic_key(7007, 8008)
        await telegram_store.bind_topic(
            telegram_key,
            str(unrelated_workspace.resolve()),
            repo_id=None,
        )

        def _enable(record: Any) -> None:
            record.pma_enabled = True
            record.repo_id = None
            record.workspace_path = str(unrelated_workspace.resolve())
            record.pma_prev_repo_id = None
            record.pma_prev_workspace_path = str(hub_env.repo_root.resolve())

        await telegram_store.update_topic(telegram_key, _enable)
    finally:
        await discord_store.close()
        await telegram_store.close()

    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:test-publish-prev-workspace"
    start_lane_worker = app.state.pma_lane_worker_start
    stop_lane_worker = app.state.pma_lane_worker_stop
    assert callable(start_lane_worker)
    assert callable(stop_lane_worker)

    item, _ = await queue.enqueue(
        lane_id,
        "pma:test-publish-prev-workspace:key-1",
        {
            "message": "Automation wake-up received.",
            "agent": "codex",
            "client_turn_id": "wakeup-prev-workspace-123",
            "wake_up": {
                "wakeup_id": "wakeup-prev-workspace-123",
                "repo_id": hub_env.repo_id,
                "event_type": "managed_thread_completed",
                "source": "lifecycle_subscription",
                "run_id": "run-prev-workspace-123",
            },
        },
    )

    try:
        await start_lane_worker(app, lane_id)
        result: dict[str, Any] | None = None
        with anyio.fail_after(3):
            while True:
                items = await queue.list_items(lane_id)
                match = next(
                    (entry for entry in items if entry.item_id == item.item_id), None
                )
                assert match is not None
                if match.state in (QueueItemState.COMPLETED, QueueItemState.FAILED):
                    result = dict(match.result or {})
                    break
                await anyio.sleep(0.05)
        assert result is not None
        assert result.get("status") == "ok"
        assert result.get("delivery_status") == "success"
    finally:
        await stop_lane_worker(app, lane_id)

    discord_store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        discord_outbox = await discord_store.list_outbox()
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await discord_store.close()
        await telegram_store.close()

    assert any(
        record.channel_id == "discord-prev-workspace" for record in discord_outbox
    )
    assert not any(
        record.chat_id == 7007 and record.thread_id == 8008
        for record in telegram_outbox
    )


def test_pma_active_clears_on_prompt_build_error(hub_env, monkeypatch) -> None:
    _enable_pma(hub_env.hub_root)

    async def _boom(*args, **kwargs):
        raise RuntimeError("snapshot failed")

    app = create_hub_app(hub_env.hub_root)
    app.state.pma_container.ports.build_hub_snapshot = _boom

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "error"
    assert "snapshot failed" in (payload.get("detail") or "")

    active = client.get("/hub/pma/active").json()
    assert active["active"] is False
    assert active["current"] == {}


def test_pma_thread_reset_clears_registry(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id(PMA_KEY, "thread-codex")
    registry.set_thread_id(PMA_OPENCODE_KEY, "thread-opencode")
    registry.set_thread_id("pma.hermes", "thread-hermes")

    client = TestClient(app)
    resp = client.post("/hub/pma/thread/reset", json={"agent": "opencode"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) == "thread-codex"
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None
    assert registry.get_thread_id("pma.hermes") == "thread-hermes"

    resp = client.post("/hub/pma/thread/reset", json={"agent": "hermes"})
    assert resp.status_code == 200
    assert registry.get_thread_id("pma.hermes") is None

    resp = client.post("/hub/pma/thread/reset", json={"agent": "all"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) is None


def test_pma_reset_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post("/hub/pma/reset", json={"agent": "all"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()


def test_pma_stop_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post("/hub/pma/stop", json={"lane_id": "pma:default"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert payload["details"]["lane_id"] == "pma:default"


def test_pma_compact_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post(
        "/hub/pma/compact",
        json={"summary": "compact this PMA history", "agent": "codex"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert payload["details"]["summary_length"] == len("compact this PMA history")


def test_pma_new_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post(
        "/hub/pma/new", json={"agent": "codex", "lane_id": "pma:default"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()


def test_pma_new_forks_current_hermes_thread_when_runtime_supports_it(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    observed: dict[str, Any] = {}

    class _HermesSupervisor:
        async def fork_session(
            self,
            workspace_root: Path,
            session_id: str,
            *,
            title: Optional[str] = None,
            metadata: Optional[dict[str, Any]] = None,
        ):
            observed["fork"] = (workspace_root, session_id, title, metadata)
            return type("Forked", (), {"session_id": "hermes-session-forked"})()

    app.state.hermes_supervisor = _HermesSupervisor()

    with TestClient(app) as client:
        registry = app.state.app_server_threads
        registry.set_thread_id("pma.hermes", "hermes-session-source")
        original_get_current_snapshot = (
            chat_runtime.PmaRuntimeState.get_current_snapshot
        )

        async def _fake_get_current_snapshot(self) -> dict[str, Any]:
            return {}

        try:
            chat_runtime.PmaRuntimeState.get_current_snapshot = _fake_get_current_snapshot  # type: ignore[assignment]
            resp = client.post(
                "/hub/pma/new",
                json={"agent": "hermes", "lane_id": "pma:default"},
            )
        finally:
            chat_runtime.PmaRuntimeState.get_current_snapshot = original_get_current_snapshot  # type: ignore[assignment]

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["details"]["forked"] is True
    assert payload["details"]["source_thread_id"] == "hermes-session-source"
    assert payload["details"]["thread_id"] == "hermes-session-forked"
    assert observed["fork"][0] == hub_env.hub_root
    assert observed["fork"][1] == "hermes-session-source"
    assert registry.get_thread_id("pma.hermes") == "hermes-session-forked"


@pytest.mark.parametrize(
    ("endpoint", "body", "unknown_key"),
    [
        ("/hub/pma/chat", {"message": "hi", "mesage": "typo"}, "mesage"),
        ("/hub/pma/stop", {"lane_id": "pma:default", "laneid": "typo"}, "laneid"),
        ("/hub/pma/new", {"agent": "codex", "laneid": "pma:default"}, "laneid"),
        ("/hub/pma/reset", {"agent": "all", "profiel": "ops"}, "profiel"),
        (
            "/hub/pma/compact",
            {"summary": "compact this PMA history", "threadid": "thread-1"},
            "threadid",
        ),
        (
            "/hub/pma/thread/reset",
            {"agent": "codex", "profiel": "ops"},
            "profiel",
        ),
    ],
)
def test_pma_runtime_routes_reject_unknown_keys(
    hub_env, endpoint: str, body: dict[str, Any], unknown_key: str
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    response = client.post(endpoint, json=body)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == unknown_key for item in detail)


def test_pma_turn_events_stream_codex_respects_resume_cursor(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeEvents:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        def stream(self, thread_id: str, turn_id: str, *, after_id: int = 0):
            self.calls.append((thread_id, turn_id, after_id))

            async def _stream():
                yield (
                    "id: 2\n"
                    "event: app-server\n"
                    'data: {"thread_id":"thread-1","turn_id":"turn-1","seq":2}\n\n'
                )

            return _stream()

    fake_events = FakeEvents()
    app.state.app_server_events = fake_events

    client = TestClient(app)
    resp = client.get(
        "/hub/pma/turns/turn-1/events",
        params={"thread_id": "thread-1"},
        headers={"Last-Event-ID": "1"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: app-server" in resp.text
    assert '"seq":2' in resp.text
    assert fake_events.calls == [("thread-1", "turn-1", 1)]


def test_pma_turn_events_stream_hermes_uses_runtime_harness(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class _HermesSupervisor:
        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def create_session(
            self, workspace_root: Path, title: Optional[str] = None
        ):
            _ = workspace_root, title
            return type("Session", (), {"session_id": "hermes-session-1"})()

        async def resume_session(self, workspace_root: Path, conversation_id: str):
            _ = workspace_root
            return type("Session", (), {"session_id": conversation_id})()

        async def start_turn(self, *_args: Any, **_kwargs: Any) -> str:
            return "hermes-turn-1"

        async def wait_for_turn(self, *_args: Any, **_kwargs: Any):
            return type(
                "Result",
                (),
                {
                    "status": "completed",
                    "assistant_text": "done",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def interrupt_turn(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def stream_turn_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {"method": "prompt/progress", "params": {"delta": "hello "}}
            yield {"method": "prompt/completed", "params": {"finalOutput": "hello"}}

    app.state.hermes_supervisor = _HermesSupervisor()

    client = TestClient(app)
    resp = client.get(
        "/hub/pma/turns/turn-1/events",
        params={"thread_id": "hermes-session-1", "agent": "hermes"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "prompt/progress" in resp.text
    assert "prompt/completed" in resp.text


def test_pma_chat_hermes_reuses_agent_scoped_registry_binding(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id("pma.hermes", "hermes-session-stored")
    observed: dict[str, Any] = {}

    class _HermesSupervisor:
        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def create_session(
            self, workspace_root: Path, title: Optional[str] = None
        ):
            _ = workspace_root, title
            raise AssertionError("should resume stored Hermes session")

        async def resume_session(self, workspace_root: Path, conversation_id: str):
            observed["resume"] = (workspace_root, conversation_id)
            return type("Session", (), {"session_id": conversation_id})()

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str] = None,
            approval_mode: Optional[str] = None,
        ) -> str:
            observed["start_turn"] = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                approval_mode,
            )
            return "hermes-turn-2"

        async def wait_for_turn(self, *_args: Any, **_kwargs: Any):
            return type(
                "Result",
                (),
                {
                    "status": "completed",
                    "assistant_text": "hermes reply",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def interrupt_turn(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def stream_turn_events(self, *_args: Any, **_kwargs: Any):
            if False:
                yield {}

    app.state.hermes_supervisor = _HermesSupervisor()

    client = TestClient(app)
    resp = client.post(
        "/hub/pma/chat", json={"message": "hello hermes", "agent": "hermes"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert observed["resume"] == (hub_env.hub_root, "hermes-session-stored")
    assert observed["start_turn"][1] == "hermes-session-stored"


def test_pma_chat_hermes_profile_uses_profile_scoped_registry_binding(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    cfg["pma"]["profile"] = "m4"
    cfg.setdefault("agents", {})
    cfg["agents"]["hermes"] = {
        "binary": "hermes",
        "profiles": {"m4": {"binary": "hermes-m4"}},
        "default_profile": "m4",
    }
    write_test_config(hub_env.hub_root / CONFIG_FILENAME, cfg)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id("pma.hermes.profile.m4", "hermes-session-m4")
    observed: dict[str, Any] = {}

    class _HermesSupervisor:
        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def create_session(
            self, workspace_root: Path, title: Optional[str] = None
        ):
            _ = workspace_root, title
            raise AssertionError("should resume stored Hermes session for profile")

        async def resume_session(self, workspace_root: Path, conversation_id: str):
            observed["resume"] = (workspace_root, conversation_id)
            return type("Session", (), {"session_id": conversation_id})()

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str] = None,
            approval_mode: Optional[str] = None,
        ) -> str:
            observed["start_turn"] = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                approval_mode,
            )
            return "hermes-turn-profile"

        async def wait_for_turn(self, *_args: Any, **_kwargs: Any):
            return type(
                "Result",
                (),
                {
                    "status": "completed",
                    "assistant_text": "hermes profile reply",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def interrupt_turn(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def stream_turn_events(self, *_args: Any, **_kwargs: Any):
            if False:
                yield {}

    fake_supervisor = _HermesSupervisor()
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.build_hermes_supervisor_from_config",
        lambda *args, **kwargs: fake_supervisor,
    )

    client = TestClient(app)
    resp = client.post(
        "/hub/pma/chat",
        json={"message": "hello hermes profile", "agent": "hermes"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert observed["resume"] == (hub_env.hub_root, "hermes-session-m4")
    assert observed["start_turn"][1] == "hermes-session-m4"


def test_pma_chat_codex_ignores_global_profile_default_when_agent_has_no_profiles(
    hub_env,
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    cfg["pma"]["profile"] = "m4"
    cfg.setdefault("agents", {})
    cfg["agents"]["hermes"] = {
        "binary": "hermes",
        "profiles": {"m4": {"binary": "hermes-m4"}},
        "default_profile": "m4",
    }
    write_test_config(hub_env.hub_root / CONFIG_FILENAME, cfg)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-codex-no-profile",
        message="codex reply",
    )

    client = TestClient(app)
    resp = client.post(
        "/hub/pma/chat",
        json={"message": "hello codex without profile", "agent": "codex"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload.get("profile") is None


def test_pma_chat_codex_retries_with_fresh_conversation_after_stale_resume(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id(PMA_KEY, "stale-codex-thread")
    observed: dict[str, Any] = {}

    class _CodexHarness:
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            observed["new_conversation"] = (
                workspace_root,
                title,
            )
            return SimpleNamespace(id="fresh-codex-thread")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            observed["resume"] = (workspace_root, conversation_id)
            return SimpleNamespace(id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            observed.setdefault("start_turn_calls", []).append(conversation_id)
            if conversation_id == "stale-codex-thread":
                raise CodexAppServerResponseError(
                    method="turn/start",
                    code=-32600,
                    message="thread not found: stale-codex-thread",
                )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="codex-turn-2",
            )

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="codex reply after stale resume recovery",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    monkeypatch.setattr(
        chat_runtime,
        "get_registered_agents",
        lambda: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=_CodexHarness.capabilities,
                make_harness=lambda _ctx: _CodexHarness(),
            )
        },
    )

    client = TestClient(app)
    resp = client.post(
        "/hub/pma/chat",
        json={"message": "hello codex", "agent": "codex"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert "codex reply after stale resume recovery" in payload["message"]
    assert observed["resume"] == (hub_env.hub_root, "stale-codex-thread")
    assert observed["start_turn_calls"] == [
        "stale-codex-thread",
        "fresh-codex-thread",
    ]
    assert registry.get_thread_id(PMA_KEY) == "fresh-codex-thread"


def test_pma_turn_events_stream_opencode_returns_empty_stream_without_pending_turn(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    app.state.opencode_supervisor = object()

    client = TestClient(app)
    resp = client.get(
        "/hub/pma/turns/turn-1/events",
        params={"thread_id": "thread-1", "agent": "opencode"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == ""


def test_pma_managed_thread_status_and_tail_use_orchestration_service(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.thread = ThreadTarget(
                thread_target_id="thread-1",
                agent_id="codex",
                backend_thread_id="backend-thread-1",
                repo_id=hub_env.repo_id,
                workspace_root=str(hub_env.repo_root.resolve()),
                display_name="Status thread",
                status="completed",
                lifecycle_status="active",
                status_reason="managed_turn_completed",
                status_changed_at="2026-03-13T00:01:00Z",
                status_terminal=True,
                status_turn_id="turn-1",
            )
            self.execution = ExecutionRecord(
                execution_id="turn-1",
                target_id="thread-1",
                target_kind="thread",
                request_kind="review",
                status="ok",
                backend_id="backend-turn-1",
                started_at="2026-03-13T00:00:00Z",
                finished_at="2026-03-13T00:01:00Z",
                output_text="assistant output from orchestration",
            )

        def get_thread_target(self, thread_target_id: str):
            self.calls.append(("get_thread_target", thread_target_id))
            if thread_target_id != self.thread.thread_target_id:
                return None
            return self.thread

        def get_running_execution(self, thread_target_id: str):
            self.calls.append(("get_running_execution", thread_target_id))
            return None

        def get_latest_execution(self, thread_target_id: str):
            self.calls.append(("get_latest_execution", thread_target_id))
            return self.execution

        def get_execution(self, thread_target_id: str, execution_id: str):
            self.calls.append(("get_execution", f"{thread_target_id}:{execution_id}"))
            return self.execution

        def get_queue_depth(self, thread_target_id: str):
            self.calls.append(("get_queue_depth", thread_target_id))
            return 0

    fake_service = FakeService()
    monkeypatch.setattr(
        tail_stream,
        "build_managed_thread_orchestration_service",
        lambda request: fake_service,
    )

    app = create_hub_app(hub_env.hub_root)
    app.state.app_server_events = None
    client = TestClient(app)

    status_resp = client.get("/hub/pma/threads/thread-1/status")
    tail_resp = client.get("/hub/pma/threads/thread-1/tail")

    assert status_resp.status_code == 200
    assert tail_resp.status_code == 200

    status_payload = status_resp.json()
    assert status_payload["thread"]["managed_thread_id"] == "thread-1"
    assert status_payload["thread"]["status"] == "completed"
    assert status_payload["turn"]["managed_turn_id"] == "turn-1"
    assert status_payload["turn"]["status"] == "ok"
    assert status_payload["queue_depth"] == 0
    assert status_payload["queued_turns"] == []
    assert (
        status_payload["latest_output_excerpt"] == "assistant output from orchestration"
    )
    assert status_payload["stream_available"] is False

    tail_payload = tail_resp.json()
    assert tail_payload["managed_thread_id"] == "thread-1"
    assert tail_payload["managed_turn_id"] == "turn-1"
    assert tail_payload["turn_status"] == "ok"
    assert tail_payload["backend_turn_id"] == "backend-turn-1"

    assert fake_service.calls[:4] == [
        ("get_thread_target", "thread-1"),
        ("get_running_execution", "thread-1"),
        ("get_latest_execution", "thread-1"),
        ("get_thread_target", "thread-1"),
    ]
    assert ("get_queue_depth", "thread-1") in fake_service.calls
    assert fake_service.calls.count(("get_running_execution", "thread-1")) >= 3


def test_pma_active_status_distinguishes_running_from_idle(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-active-1",
        message="active response",
    )
    app.state.app_server_events = object()

    client = TestClient(app)

    idle_resp = client.get("/hub/pma/active")
    assert idle_resp.status_code == 200
    idle_payload = idle_resp.json()
    assert idle_payload["active"] is False
    assert idle_payload.get("current") == {} or not idle_payload.get("current", {}).get(
        "thread_id"
    )


def test_pma_active_status_surfaces_last_result_on_completion(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    _install_fake_successful_chat_supervisor(
        app,
        turn_id="turn-result-1",
        message="completed response text",
    )
    app.state.app_server_events = object()

    client = TestClient(app)
    chat_resp = client.post(
        "/hub/pma/chat",
        json={
            "message": "hello",
            "stream": False,
            "client_turn_id": "cturn-result-1",
        },
    )
    assert chat_resp.status_code == 200

    active_resp = client.get("/hub/pma/active")
    assert active_resp.status_code == 200
    active_payload = active_resp.json()
    assert active_payload["active"] is False
    last = active_payload.get("last_result", {})
    assert last.get("status") == "ok"
    assert last.get("message") == "completed response text"


@pytest.mark.anyio
async def test_pma_queue_endpoint_provides_lane_items_for_thread_info(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    queue = PmaQueue(hub_env.hub_root)
    lane_id = "pma:default"

    for i in range(3):
        await queue.enqueue(
            lane_id,
            f"pma:default:queue-info-{i}",
            {"message": f"turn {i}", "agent": "codex"},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        lane_resp = await client.get(f"/hub/pma/queue/{lane_id}")
        summary_resp = await client.get("/hub/pma/queue")

    assert lane_resp.status_code == 200
    lane_payload = lane_resp.json()
    pending_items = [
        item for item in lane_payload["items"] if item["state"] == "pending"
    ]
    assert len(pending_items) == 3
    for item in pending_items:
        assert item["enqueued_at"] is not None

    assert summary_resp.status_code == 200
    summary_payload = summary_resp.json()
    assert summary_payload["lanes"][lane_id]["by_state"]["pending"] == 3


def test_pma_managed_thread_status_includes_phase_and_guidance(hub_env) -> None:
    _enable_pma(hub_root=hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    store.create_turn(managed_thread_id, prompt="test phase")

    client = TestClient(app)
    resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

    assert resp.status_code == 200
    payload = resp.json()
    turn = payload.get("turn", {})
    assert "phase" in turn
    assert "guidance" in turn
    assert "elapsed_seconds" in turn
    assert "activity" in turn
