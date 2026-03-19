import asyncio
import concurrent.futures
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import pma_active_context_content, seed_hub_files
from codex_autorunner.core import filebox
from codex_autorunner.core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration import ExecutionRecord, ThreadTarget
from codex_autorunner.core.pma_context import maybe_auto_prune_active_context
from codex_autorunner.core.pma_queue import PmaQueue, QueueItemState
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore, topic_key
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import pma as pma_routes
from codex_autorunner.surfaces.web.routes.pma_routes import chat_runtime, tail_stream
from tests.conftest import write_test_config

pytestmark = pytest.mark.slow


def _enable_pma(
    hub_root: Path,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    max_text_chars: Optional[int] = None,
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    if max_text_chars is not None:
        cfg["pma"]["max_text_chars"] = max_text_chars
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _disable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _install_fake_successful_chat_supervisor(
    app,
    *,
    turn_id: str,
    message: str = "assistant text",
    raw_events: Optional[list[dict]] = None,
) -> None:
    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = turn_id

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {
                    "agent_messages": [message],
                    "raw_events": list(raw_events or []),
                    "errors": [],
                },
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
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()


async def _seed_discord_pma_binding(hub_env, *, channel_id: str) -> None:
    store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        await store.upsert_binding(
            channel_id=channel_id,
            guild_id="guild-1",
            workspace_path=str(hub_env.repo_root.resolve()),
            repo_id=hub_env.repo_id,
        )
        await store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=True,
            pma_prev_workspace_path=str(hub_env.repo_root.resolve()),
            pma_prev_repo_id=hub_env.repo_id,
        )
    finally:
        await store.close()


async def _seed_telegram_pma_binding(
    hub_env, *, chat_id: int, thread_id: Optional[int]
) -> None:
    store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        key = topic_key(chat_id, thread_id)
        await store.bind_topic(
            key,
            str(hub_env.repo_root.resolve()),
            repo_id=hub_env.repo_id,
        )

        def _enable(record: Any) -> None:
            record.pma_enabled = True
            record.pma_prev_repo_id = hub_env.repo_id
            record.pma_prev_workspace_path = str(hub_env.repo_root.resolve())

        await store.update_topic(key, _enable)
    finally:
        await store.close()


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


def test_pma_thread_status_includes_queued_turns(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
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
    assert payload["queued_turns"][0]["prompt_preview"] == "second"


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


def test_pma_chat_github_injection_uses_raw_user_message(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    observed: dict[str, str] = {}

    async def _fake_github_context_injection(**kwargs):
        observed["link_source_text"] = str(kwargs.get("link_source_text") or "")
        prompt_text = str(kwargs.get("prompt_text") or "")
        return f"{prompt_text}\n\n[injected-from-github]", True

    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.pma.maybe_inject_github_context",
        _fake_github_context_injection,
    )
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
    assert any(
        record.chat_id == 1001 and record.thread_id == 2002
        for record in telegram_outbox
    )
    assert any(
        "automation summary complete" in record.text for record in telegram_outbox
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
    original_sleep = pma_routes.asyncio.sleep
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
    monkeypatch.setattr(pma_routes.asyncio, "sleep", _fake_sleep)

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
        assert outcome.get("failed") == 0
    finally:
        await stop_lane_worker(app, lane_id)

    assert enqueue_attempts == 2
    assert sleep_calls == [0.25]

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


def test_pma_active_clears_on_prompt_build_error(hub_env, monkeypatch) -> None:
    _enable_pma(hub_env.hub_root)

    async def _boom(*args, **kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.pma.build_hub_snapshot",
        _boom,
    )
    app = create_hub_app(hub_env.hub_root)

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

    client = TestClient(app)
    resp = client.post("/hub/pma/thread/reset", json={"agent": "opencode"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) == "thread-codex"
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None

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


def test_pma_files_list_empty(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []
    assert payload["outbox"] == []


def test_pma_files_upload_list_download_delete(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to inbox
    files = {"file.txt": ("file.txt", b"Hello, PMA!", "text/plain")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["inbox"]) == 1
    assert payload["inbox"][0]["name"] == "file.txt"
    assert payload["inbox"][0]["box"] == "inbox"
    assert payload["inbox"][0]["size"] == 11
    assert payload["inbox"][0]["source"] == "filebox"
    assert "/hub/pma/files/inbox/file.txt" in payload["inbox"][0]["url"]
    assert payload["outbox"] == []
    assert (
        filebox.inbox_dir(hub_env.hub_root) / "file.txt"
    ).read_bytes() == b"Hello, PMA!"

    # Download file
    resp = client.get("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200
    assert resp.content == b"Hello, PMA!"

    # Delete file
    resp = client.delete("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200

    # Verify file is gone
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []


def test_pma_files_invalid_box(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Try to upload to invalid box
    files = {"file.txt": ("file.txt", b"test", "text/plain")}
    resp = client.post("/hub/pma/files/invalid", files=files)
    assert resp.status_code == 400

    # Try to download from invalid box
    resp = client.get("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400

    # Try to delete from invalid box
    resp = client.delete("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400


def test_pma_files_list_ignores_legacy_sources(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.inbox_dir(hub_env.hub_root) / "primary.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "legacy-pma.txt").write_bytes(b"legacy-pma")
    legacy_telegram = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "inbox"
    )
    legacy_telegram.mkdir(parents=True, exist_ok=True)
    (legacy_telegram / "legacy-telegram.txt").write_bytes(b"legacy-telegram")

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == [
        {
            "box": "inbox",
            "item_type": "pma_file",
            "modified_at": payload["inbox"][0]["modified_at"],
            "name": "primary.txt",
            "next_action": "process_uploaded_file",
            "size": 7,
            "source": "filebox",
            "url": payload["inbox"][0]["url"],
        }
    ]


def test_pma_files_download_rejects_legacy_path(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "legacy.txt").write_bytes(b"legacy")

    resp = client.get("/hub/pma/files/inbox/legacy.txt")
    assert resp.status_code == 404


def test_pma_files_outbox(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to outbox
    files = {"output.txt": ("output.txt", b"Output content", "text/plain")}
    resp = client.post("/hub/pma/files/outbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["outbox"]) == 1
    assert payload["outbox"][0]["name"] == "output.txt"
    assert payload["outbox"][0]["box"] == "outbox"
    assert "/hub/pma/files/outbox/output.txt" in payload["outbox"][0]["url"]
    assert payload["inbox"] == []

    # Download from outbox
    resp = client.get("/hub/pma/files/outbox/output.txt")
    assert resp.status_code == 200
    assert resp.content == b"Output content"


def test_pma_files_delete_removes_only_resolved_file(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.inbox_dir(hub_env.hub_root) / "shared.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "inbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "shared.txt").write_bytes(b"legacy-pma")
    legacy_telegram = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-2"
        / "inbox"
    )
    legacy_telegram.mkdir(parents=True, exist_ok=True)
    (legacy_telegram / "shared.txt").write_bytes(b"legacy-telegram")

    resp = client.delete("/hub/pma/files/inbox/shared.txt")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.inbox_dir(hub_env.hub_root) / "shared.txt").exists()
    assert (legacy_pma / "shared.txt").exists()
    assert (legacy_telegram / "shared.txt").exists()


def test_pma_files_bulk_delete_removes_only_canonical_entries(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.outbox_dir(hub_env.hub_root) / "a.txt").write_bytes(b"a")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "outbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "b.txt").write_bytes(b"b")
    legacy_telegram_pending = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-3"
        / "outbox"
        / "pending"
    )
    legacy_telegram_pending.mkdir(parents=True, exist_ok=True)
    (legacy_telegram_pending / "c.txt").write_bytes(b"c")

    resp = client.delete("/hub/pma/files/outbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (client.get("/hub/pma/files").json()["outbox"]) == []
    assert not (filebox.outbox_dir(hub_env.hub_root) / "a.txt").exists()
    assert (legacy_pma / "b.txt").exists()
    assert (legacy_telegram_pending / "c.txt").exists()


def test_pma_files_bulk_delete_leaves_legacy_duplicates_hidden(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    (filebox.outbox_dir(hub_env.hub_root) / "shared.txt").write_bytes(b"primary")
    legacy_pma = hub_env.hub_root / ".codex-autorunner" / "pma" / "outbox"
    legacy_pma.mkdir(parents=True, exist_ok=True)
    (legacy_pma / "shared.txt").write_bytes(b"legacy")

    resp = client.delete("/hub/pma/files/outbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.outbox_dir(hub_env.hub_root) / "shared.txt").exists()
    assert (legacy_pma / "shared.txt").exists()
    payload = client.get("/hub/pma/files").json()
    assert payload["outbox"] == []


def test_pma_files_list_waits_for_bulk_delete_to_finish(hub_env, monkeypatch) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.hub_root)
    race_file = filebox.inbox_dir(hub_env.hub_root) / "race.txt"
    race_file.write_bytes(b"race")

    unlink_started = threading.Event()
    release_unlink = threading.Event()
    original_unlink = Path.unlink

    def _gated_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == race_file and not unlink_started.is_set():
            unlink_started.set()
            if not release_unlink.wait(timeout=3):
                raise AssertionError("Timed out waiting to release bulk delete unlink")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _gated_unlink)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(lambda: client.delete("/hub/pma/files/inbox"))
        assert unlink_started.wait(timeout=3), "Bulk delete did not reach unlink gate"
        list_future = executor.submit(lambda: client.get("/hub/pma/files"))
        release_unlink.set()

        delete_resp = delete_future.result(timeout=3)
        list_resp = list_future.result(timeout=3)

    assert delete_resp.status_code == 200
    assert list_resp.status_code == 200
    assert list_resp.json()["inbox"] == []


def test_pma_files_rejects_invalid_filenames(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Test traversal attempts - upload rejects invalid filenames
    for filename in ["../x", "..", "a/b", "a\\b", ".", ""]:
        files = {"file": (filename, b"test", "text/plain")}
        resp = client.post("/hub/pma/files/inbox", files=files)
        assert resp.status_code == 400, f"Should reject filename: {filename}"
        assert "filename" in resp.json()["detail"].lower()


def test_pma_files_size_limit(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    max_upload_bytes = DEFAULT_HUB_CONFIG["pma"]["max_upload_bytes"]

    # Upload a file that exceeds the size limit
    large_content = b"x" * (max_upload_bytes + 1)
    files = {"large.bin": ("large.bin", large_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()

    # Upload a file that is exactly at the limit
    limit_content = b"y" * max_upload_bytes
    files = {"limit.bin": ("limit.bin", limit_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200
    assert "limit.bin" in resp.json()["saved"]


def test_pma_files_returns_404_for_nonexistent_files(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Download non-existent file
    resp = client.get("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]

    # Delete non-existent file
    resp = client.delete("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]


def test_pma_docs_list(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    assert "docs" in payload
    docs = payload["docs"]
    assert isinstance(docs, list)
    doc_names = [doc["name"] for doc in docs]
    assert doc_names == [
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    ]
    for doc in docs:
        assert "name" in doc
        assert "exists" in doc
        if doc["exists"]:
            assert "size" in doc
            assert "mtime" in doc
        if doc["name"] == "active_context.md":
            assert "line_count" in doc


def test_pma_docs_list_includes_auto_prune_metadata(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    active_context = (
        hub_env.hub_root / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    active_context.write_text(
        "\n".join(f"line {idx}" for idx in range(220)), encoding="utf-8"
    )
    state = maybe_auto_prune_active_context(hub_env.hub_root, max_lines=50)
    assert state is not None

    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    meta = payload.get("active_context_auto_prune")
    assert isinstance(meta, dict)
    assert meta.get("line_count_before") == 220
    assert meta.get("line_budget") == 50
    assert isinstance(meta.get("last_auto_pruned_at"), str)


def test_pma_docs_get(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert "content" in payload
    assert isinstance(payload["content"], str)


def test_pma_docs_get_nonexistent(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Delete the canonical doc, then try to get it
    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_agents_path = pma_dir / "docs" / "AGENTS.md"
    if docs_agents_path.exists():
        docs_agents_path.unlink()

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_pma_docs_list_migrates_legacy_doc_into_canonical(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_path = pma_dir / "docs" / "active_context.md"
    legacy_path = pma_dir / "active_context.md"

    docs_path.unlink(missing_ok=True)
    legacy_content = "# Legacy copy\n\n- migrated\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200

    assert docs_path.exists()
    assert docs_path.read_text(encoding="utf-8") == legacy_content
    assert not legacy_path.exists()

    resp = client.get("/hub/pma/docs/active_context.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == legacy_content


def test_pma_docs_put(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    new_content = "# AGENTS\n\nNew content"
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": new_content})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert payload["status"] == "ok"

    # Verify the content was saved
    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == new_content


def test_pma_docs_put_writes_via_to_thread(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    to_thread_calls: list[tuple[object, tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(pma_routes.asyncio, "to_thread", _fake_to_thread)

    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": "# AGENTS\n\nText"})
    assert resp.status_code == 200
    assert any(func is pma_routes.atomic_write for func, _, _ in to_thread_calls)


def test_pma_docs_put_invalid_name(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/invalid.md", json={"content": "test"})
    assert resp.status_code == 400
    assert "Unknown doc name" in resp.json()["detail"]


def test_pma_docs_put_too_large(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    large_content = "x" * 500_001
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": large_content})
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_pma_docs_put_invalid_content_type(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": 123})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI returns a list of validation errors
    if isinstance(detail, list):
        assert any("content" in str(err) for err in detail)
    else:
        assert "content" in str(detail)


def test_pma_context_snapshot(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_path = docs_dir / "active_context.md"
    active_content = "# Active Context\n\n- alpha\n- beta\n"
    active_path.write_text(active_content, encoding="utf-8")

    resp = client.post("/hub/pma/context/snapshot", json={"reset": True})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["active_context_line_count"] == len(active_content.splitlines())
    assert payload["reset"] is True

    log_content = (docs_dir / "context_log.md").read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content
    assert active_content in log_content
    assert active_path.read_text(encoding="utf-8") == pma_active_context_content()


def test_pma_context_snapshot_writes_via_to_thread(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    to_thread_calls: list[tuple[object, tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(pma_routes.asyncio, "to_thread", _fake_to_thread)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_path = docs_dir / "active_context.md"
    active_path.write_text("# Active Context\n\n- alpha\n", encoding="utf-8")

    resp = client.post("/hub/pma/context/snapshot", json={"reset": True})
    assert resp.status_code == 200
    assert any(func is pma_routes.ensure_pma_docs for func, _, _ in to_thread_calls)
    assert any(func is pma_routes.atomic_write for func, _, _ in to_thread_calls)


def test_pma_docs_disabled(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 404

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404


def test_pma_automation_subscription_endpoints(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []
            self.list_filters: list[dict[str, Any]] = []
            self.deleted_ids: list[str] = []

        def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"subscription_id": "sub-1", **payload}

        def list_subscriptions(self, **filters: Any) -> list[dict[str, Any]]:
            self.list_filters.append(dict(filters))
            return [{"subscription_id": "sub-1", "thread_id": "thread-1"}]

        def delete_subscription(self, subscription_id: str) -> dict[str, Any]:
            self.deleted_ids.append(subscription_id)
            return {"deleted": True}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "thread_id": "thread-1",
                "from_state": "running",
                "to_state": "completed",
                "reason": "manual",
                "timestamp": "2026-03-01T12:00:00Z",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["subscription"]
        assert created["subscription_id"] == "sub-1"
        assert created["thread_id"] == "thread-1"

        list_resp = client.get(
            "/hub/pma/automation/subscriptions",
            params={"thread_id": "thread-1", "limit": 5},
        )
        assert list_resp.status_code == 200
        listed = list_resp.json()["subscriptions"]
        assert listed and listed[0]["subscription_id"] == "sub-1"

        delete_resp = client.delete("/hub/pma/subscriptions/sub-1")
        assert delete_resp.status_code == 200
        assert delete_resp.json()["status"] == "ok"
        assert delete_resp.json()["subscription_id"] == "sub-1"

    assert fake_store.created_payloads
    assert fake_store.created_payloads[0]["from_state"] == "running"
    assert fake_store.created_payloads[0]["to_state"] == "completed"
    assert (
        fake_store.list_filters
        and fake_store.list_filters[0]["thread_id"] == "thread-1"
    )
    assert fake_store.deleted_ids == ["sub-1"]


def test_pma_automation_timer_endpoints(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []
            self.list_filters: list[dict[str, Any]] = []
            self.touched: list[tuple[str, dict[str, Any]]] = []
            self.cancelled: list[tuple[str, dict[str, Any]]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "timer-1", **payload}

        def list_timers(self, **filters: Any) -> list[dict[str, Any]]:
            self.list_filters.append(dict(filters))
            return [{"timer_id": "timer-1", "thread_id": "thread-1"}]

        def touch_timer(self, timer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.touched.append((timer_id, dict(payload)))
            return {"timer_id": timer_id, "touched": True}

        def cancel_timer(
            self, timer_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            self.cancelled.append((timer_id, dict(payload)))
            return {"timer_id": timer_id, "cancelled": True}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "delay_seconds": 1800,
                "lane_id": "pma:lane-next",
                "thread_id": "thread-1",
                "from_state": "running",
                "to_state": "failed",
                "reason": "timeout",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["timer"]
        assert created["timer_id"] == "timer-1"
        assert created["thread_id"] == "thread-1"

        list_resp = client.get(
            "/hub/pma/automation/timers",
            params={"thread_id": "thread-1", "limit": 20},
        )
        assert list_resp.status_code == 200
        listed = list_resp.json()["timers"]
        assert listed and listed[0]["timer_id"] == "timer-1"

        touch_resp = client.post(
            "/hub/pma/timers/timer-1/touch",
            json={"reason": "heartbeat"},
        )
        assert touch_resp.status_code == 200
        assert touch_resp.json()["timer_id"] == "timer-1"

        cancel_resp = client.post(
            "/hub/pma/timers/timer-1/cancel",
            json={"reason": "done"},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["timer_id"] == "timer-1"

    assert fake_store.created_payloads
    assert fake_store.created_payloads[0]["timer_type"] == "one_shot"
    assert fake_store.created_payloads[0]["delay_seconds"] == 1800
    assert fake_store.created_payloads[0]["lane_id"] == "pma:lane-next"
    assert fake_store.created_payloads[0]["to_state"] == "failed"
    assert (
        fake_store.list_filters
        and fake_store.list_filters[0]["thread_id"] == "thread-1"
    )
    assert fake_store.touched == [("timer-1", {"reason": "heartbeat"})]
    assert fake_store.cancelled == [("timer-1", {"reason": "done"})]


def test_pma_automation_subscription_alias_endpoint_supports_kwargs_only_store(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.create_calls: list[dict[str, Any]] = []
            self.deleted_ids: list[str] = []

        def create_subscription(
            self, *, thread_id: Optional[str] = None, lane_id: Optional[str] = None
        ) -> dict[str, Any]:
            self.create_calls.append({"thread_id": thread_id, "lane_id": lane_id})
            return {
                "subscription_id": "sub-alias-1",
                "thread_id": thread_id,
                "lane_id": lane_id,
            }

        def delete_subscription(self, subscription_id: str) -> bool:
            self.deleted_ids.append(subscription_id)
            return True

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/automation/subscriptions",
            json={"thread_id": "thread-alias", "lane_id": "pma:lane-alias"},
        )
        assert create_resp.status_code == 200
        subscription = create_resp.json()["subscription"]
        assert subscription["subscription_id"] == "sub-alias-1"
        assert subscription["thread_id"] == "thread-alias"
        assert subscription["lane_id"] == "pma:lane-alias"

        delete_resp = client.delete("/hub/pma/automation/subscriptions/sub-alias-1")
        assert delete_resp.status_code == 200
        delete_payload = delete_resp.json()
        assert delete_payload["status"] == "ok"
        assert delete_payload["subscription_id"] == "sub-alias-1"

    assert fake_store.create_calls == [
        {"thread_id": "thread-alias", "lane_id": "pma:lane-alias"}
    ]
    assert fake_store.deleted_ids == ["sub-alias-1"]


def test_pma_automation_timer_alias_endpoint_supports_fallback_method_signatures(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.create_calls: list[dict[str, Any]] = []
            self.cancelled_ids: list[str] = []

        def create_timer(
            self,
            *,
            timer_type: Optional[str] = None,
            idle_seconds: Optional[int] = None,
        ) -> dict[str, Any]:
            self.create_calls.append(
                {"timer_type": timer_type, "idle_seconds": idle_seconds}
            )
            return {
                "timer_id": "timer-alias-1",
                "timer_type": timer_type,
                "idle_seconds": idle_seconds,
            }

        def cancel_timer(self, timer_id: str) -> bool:
            self.cancelled_ids.append(timer_id)
            return True

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/automation/timers",
            json={"timer_type": "watchdog", "idle_seconds": 45},
        )
        assert create_resp.status_code == 200
        timer = create_resp.json()["timer"]
        assert timer["timer_id"] == "timer-alias-1"
        assert timer["timer_type"] == "watchdog"
        assert timer["idle_seconds"] == 45

        cancel_resp = client.delete("/hub/pma/automation/timers/timer-alias-1")
        assert cancel_resp.status_code == 200
        cancel_payload = cancel_resp.json()
        assert cancel_payload["status"] == "ok"
        assert cancel_payload["timer_id"] == "timer-alias-1"

    assert fake_store.create_calls == [{"timer_type": "watchdog", "idle_seconds": 45}]
    assert fake_store.cancelled_ids == ["timer-alias-1"]


def test_pma_automation_watchdog_timer_create(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "watchdog-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "watchdog",
                "idle_seconds": 300,
                "thread_id": "thread-1",
                "reason": "watchdog_stalled",
            },
        )
        assert create_resp.status_code == 200
        payload = create_resp.json()["timer"]
        assert payload["timer_id"] == "watchdog-1"

    assert fake_store.created_payloads
    assert fake_store.created_payloads[0]["timer_type"] == "watchdog"
    assert fake_store.created_payloads[0]["idle_seconds"] == 300


def test_pma_automation_timer_rejects_invalid_due_at(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "timer-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/hub/pma/timers",
            json={"timer_type": "one_shot", "due_at": "not-a-timestamp"},
        )
        assert response.status_code == 422

    assert fake_store.created_payloads == []


def test_pma_orchestration_service_integration_for_thread_operations(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class FakeService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_thread_target(self, thread_target_id: str):
            self.calls.append(f"get_thread_target:{thread_target_id}")
            return None

        def get_running_execution(self, thread_target_id: str):
            self.calls.append(f"get_running_execution:{thread_target_id}")
            return None

        def get_latest_execution(self, thread_target_id: str):
            self.calls.append(f"get_latest_execution:{thread_target_id}")
            return None

    fake_service = FakeService()
    monkeypatch.setattr(
        tail_stream,
        "build_managed_thread_orchestration_service",
        lambda request: fake_service,
    )
    app = create_hub_app(hub_env.hub_root)

    client = TestClient(app)

    client.get("/hub/pma/threads/thread-1/status")
    assert any(
        call.startswith("get_thread_target:thread-1") for call in fake_service.calls
    )

    fake_service.calls.clear()
    client.get("/hub/pma/threads/thread-1/tail")
    assert any(
        call.startswith("get_thread_target:thread-1") for call in fake_service.calls
    )
