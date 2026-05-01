from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.agents.hermes.harness import HermesHarness
from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.ports.run_event import OutputDelta
from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes import tail_stream
from codex_autorunner.surfaces.web.routes.pma_routes.managed_thread_tail_serializers import (
    _refresh_active_turn_diagnostics,
)
from tests.pma_support import _enable_pma
from tests.pma_support.managed_threads import FakeZeroClawEventSupervisor

pytestmark = pytest.mark.slow


def _seed_managed_thread_with_events(hub_env, app) -> tuple[str, str]:
    return _seed_running_managed_thread(
        hub_env,
        app,
        agent="codex",
        backend_thread_id="backend-thread-1",
        backend_turn_id="backend-turn-1",
        name="tail-test",
    )


def _seed_running_managed_thread(
    hub_env,
    app,
    *,
    agent: str,
    backend_thread_id: str,
    backend_turn_id: str | None,
    name: str,
) -> tuple[str, str]:
    store = PmaThreadStore(hub_env.hub_root)
    thread = store.create_thread(
        agent=agent,
        workspace_root=hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        name=name,
    )
    managed_thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="tail prompt")
    managed_turn_id = str(turn["managed_turn_id"])
    store.set_thread_backend_id(managed_thread_id, backend_thread_id)
    if isinstance(backend_turn_id, str) and backend_turn_id:
        store.set_turn_backend_turn_id(managed_turn_id, backend_turn_id)

    events = AppServerEventBuffer(max_events_per_turn=10)
    app.state.app_server_events = events
    return managed_thread_id, managed_turn_id


def test_managed_thread_tail_snapshot_redacts_and_supports_cursor(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "echo sk-abcdefghijklmnopqrstuvwxyz123456",
                    },
                },
            }
        )
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "echo done",
                        "exitCode": 0,
                    },
                },
            }
        )

    import asyncio

    asyncio.run(_seed())

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["turn_status"] == "running"
        assert payload["activity"] in {"running", "stalled"}
        assert isinstance(payload["events"], list)
        assert len(payload["events"]) == 2
        first = payload["events"][0]
        assert first["event_id"] == 1
        assert payload["active_turn_diagnostics"]["request_kind"] == "message"
        assert payload["active_turn_diagnostics"]["prompt_preview"] == "tail prompt"
        assert payload["active_turn_diagnostics"]["last_event_type"] == "tool_completed"
        assert payload["active_turn_diagnostics"]["stalled"] is False
        rendered_first = json.dumps(first, ensure_ascii=True)
        assert "sk-[REDACTED]" in rendered_first

        debug_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail",
            params={"level": "debug"},
        )
        assert debug_resp.status_code == 200
        debug_payload = debug_resp.json()
        debug_first = debug_payload["events"][0]
        raw = json.dumps(debug_first.get("raw", {}), ensure_ascii=True)
        assert "sk-[REDACTED]" in raw
        assert "abcdefghijklmnopqrstuvwxyz123456" not in raw

        cursor_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail",
            params={"since_event_id": 1},
        )
        assert cursor_resp.status_code == 200
        cursor_payload = cursor_resp.json()
        assert [event["event_id"] for event in cursor_payload["events"]] == [2]


def test_managed_thread_tail_snapshot_suppresses_noisy_agent_delta_events(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "delta": [{"type": "output_text_delta", "text": "partial"}],
                },
            }
        )
        await events.handle_notification(
            {
                "method": "turn/plan/updated",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "plan": [{"text": "step"}],
                },
            }
        )
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {
                        "type": "agentMessage",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
            }
        )
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "status-check"},
                },
            }
        )

    import asyncio

    asyncio.run(_seed())

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

    assert resp.status_code == 200
    payload = resp.json()
    summaries = [str(event.get("summary") or "") for event in payload["events"]]
    assert summaries == ["tool: status-check"]


def test_managed_thread_tail_snapshot_treats_suppressed_agent_delta_as_activity(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)

    now = datetime.now(timezone.utc)
    delta_received_at_ms = int((now - timedelta(seconds=5)).timestamp() * 1000)
    tool_received_at_ms = int((now - timedelta(seconds=40)).timestamp() * 1000)

    class FakeEvents:
        async def list_events(
            self,
            thread_id: str,
            turn_id: str,
            *,
            after_id: int = 0,
            limit: int | None = None,
        ):
            _ = thread_id, turn_id, after_id, limit
            return [
                {
                    "id": 1,
                    "received_at": tool_received_at_ms,
                    "message": {
                        "method": "item/completed",
                        "params": {
                            "item": {"type": "tool", "name": "older-tool"},
                        },
                    },
                },
                {
                    "id": 2,
                    "received_at": delta_received_at_ms,
                    "message": {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "delta": [
                                {"type": "output_text_delta", "text": "still streaming"}
                            ]
                        },
                    },
                },
            ]

    app.state.app_server_events = FakeEvents()

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

    assert resp.status_code == 200
    payload = resp.json()
    assert [str(event.get("summary") or "") for event in payload["events"]] == [
        "tool: older-tool"
    ]
    assert payload["last_activity_at"] is not None
    assert payload["idle_seconds"] is not None
    assert int(payload["idle_seconds"]) < 15
    assert payload["phase"] not in {"no_stream_available", "likely_hung"}
    assert payload["activity"] == "running"
    assert payload["active_turn_diagnostics"]["stalled"] is False


def test_managed_thread_tail_snapshot_marks_hermes_stream_available(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: HermesHarness(object()),
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, _ = _seed_running_managed_thread(
            hub_env,
            app,
            agent="hermes",
            backend_thread_id="hermes-session-1",
            backend_turn_id="hermes-turn-1",
            name="hermes tail",
        )
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["agent"] == "hermes"
    assert payload["stream_available"] is True
    assert payload["events"] == []
    assert payload["phase"] == "booting_runtime"


def test_managed_thread_tail_snapshot_marks_opencode_stream_available(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class _OpenCodeHarnessEmptyStream:
        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

        def allows_parallel_event_stream(self) -> bool:
            return True

        async def list_progress_events(
            self, conversation_id: str, turn_id: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            _ = conversation_id, turn_id, kwargs
            return []

        def stream_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id

            async def _stream():
                if False:
                    yield None

            return _stream()

    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: _OpenCodeHarnessEmptyStream(),
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, _ = _seed_running_managed_thread(
            hub_env,
            app,
            agent="opencode",
            backend_thread_id="opencode-session-snap",
            backend_turn_id="opencode-turn-snap",
            name="opencode tail",
        )
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["agent"] == "opencode"
    assert payload["stream_available"] is True
    assert payload["events"] == []
    assert payload["phase"] == "booting_runtime"
    assert payload["phase"] != "no_stream_available"


def test_managed_thread_tail_events_streams_hermes_runtime_events(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class _HermesSupervisor:
        async def stream_turn_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id
            yield {"method": "prompt/progress", "params": {"delta": "hello "}}
            yield {"method": "prompt/completed", "params": {"finalOutput": "hello"}}

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
                    "assistant_text": "hello",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def interrupt_turn(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    supervisor = _HermesSupervisor()
    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: HermesHarness(supervisor),
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, _ = _seed_running_managed_thread(
            hub_env,
            app,
            agent="hermes",
            backend_thread_id="hermes-session-1",
            backend_turn_id="hermes-turn-1",
            name="hermes events",
        )
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail/events")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "assistant_update" in resp.text
    assert "turn_completed" in resp.text


def test_managed_thread_tail_events_reuses_active_harness_state(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class _StatefulHarness:
        def __init__(self, events: list[dict[str, Any]]) -> None:
            self._events = list(events)

        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

        def allows_parallel_event_stream(self) -> bool:
            return True

        def stream_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id

            async def _stream():
                for event in self._events:
                    yield event

            return _stream()

    harnesses = [
        _StatefulHarness(
            [
                {
                    "method": "prompt/progress",
                    "params": {"delta": "tail-update"},
                }
            ]
        ),
        _StatefulHarness([]),
    ]
    factory_calls: list[str] = []

    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: (
            factory_calls.append(agent_id)
            or harnesses[min(len(factory_calls) - 1, len(harnesses) - 1)]
        ),
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, _ = _seed_running_managed_thread(
            hub_env,
            app,
            agent="opencode",
            backend_thread_id="opencode-session-1",
            backend_turn_id="opencode-turn-1",
            name="opencode events",
        )
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail/events")

    assert resp.status_code == 200
    assert factory_calls == ["opencode"]
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: tail" in resp.text
    assert "assistant_update" in resp.text


def test_managed_thread_tail_snapshot_includes_opencode_list_progress_events(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    buffered_raw = [
        {"method": "prompt/progress", "params": {"text": "Working..."}},
    ]

    class _OpenCodeHarnessWithListProgress:
        def __init__(self, events: list[dict[str, Any]]) -> None:
            self._events = list(events)

        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

        def allows_parallel_event_stream(self) -> bool:
            return True

        def stream_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id

            async def _stream():
                for event in self._events:
                    yield event

            return _stream()

        async def list_progress_events(
            self, conversation_id: str, turn_id: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            _ = conversation_id, turn_id, kwargs
            return list(self._events)

    harness = _OpenCodeHarnessWithListProgress(buffered_raw)
    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: harness,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, _ = _seed_running_managed_thread(
            hub_env,
            app,
            agent="opencode",
            backend_thread_id="opencode-session-list",
            backend_turn_id="opencode-turn-list",
            name="opencode list progress",
        )
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["stream_available"] is True
    assert len(payload["events"]) >= 1
    first = payload["events"][0]
    assert first.get("event_type") == "assistant_update"
    assert "Working" in str(first.get("summary") or "")


def test_managed_thread_tail_snapshot_prefers_persisted_live_timeline_for_opencode(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class _OpenCodeHarnessWithoutBufferedProgress:
        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

        def allows_parallel_event_stream(self) -> bool:
            return True

        async def list_progress_events(
            self, conversation_id: str, turn_id: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            _ = conversation_id, turn_id, kwargs
            return []

        def stream_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id

            async def _stream():
                if False:
                    yield None

            return _stream()

    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: _OpenCodeHarnessWithoutBufferedProgress(),
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, managed_turn_id = _seed_running_managed_thread(
            hub_env,
            app,
            agent="opencode",
            backend_thread_id="opencode-session-live",
            backend_turn_id="opencode-turn-live",
            name="opencode persisted timeline",
        )
        persist_turn_timeline(
            hub_env.hub_root,
            execution_id=managed_turn_id,
            target_kind="thread_target",
            target_id=managed_thread_id,
            repo_id=hub_env.repo_id,
            metadata={"agent": "opencode", "status": "running"},
            events=[
                OutputDelta(
                    timestamp="2026-04-06T10:00:00Z",
                    content="Working through the issue...",
                    delta_type="assistant_message",
                )
            ],
        )
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["stream_available"] is True
    assert [event["event_id"] for event in payload["events"]] == [1]
    assert payload["events"][0]["event_type"] == "assistant_update"
    assert "Working through the issue" in str(payload["events"][0]["summary"] or "")
    assert payload["last_event_id"] == 1
    assert payload["last_event_at"] == "2026-04-06T10:00:00Z"


def test_managed_thread_tail_snapshot_stream_available_when_backend_binding_appears(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class _OpenCodeHarnessEmptyStream:
        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

        def allows_parallel_event_stream(self) -> bool:
            return True

        async def list_progress_events(
            self, conversation_id: str, turn_id: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            _ = conversation_id, turn_id, kwargs
            return []

        def stream_events(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ):
            _ = workspace_root, conversation_id, turn_id

            async def _stream():
                if False:
                    yield None

            return _stream()

    monkeypatch.setattr(
        tail_stream,
        "_managed_thread_harness",
        lambda service, agent_id: _OpenCodeHarnessEmptyStream(),
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        managed_thread_id, managed_turn_id = _seed_running_managed_thread(
            hub_env,
            app,
            agent="opencode",
            backend_thread_id="opencode-session-bind",
            backend_turn_id=None,
            name="opencode delayed bind",
        )
        resp_before = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")
        assert resp_before.status_code == 200
        before = resp_before.json()
        assert before["stream_available"] is False

        store = PmaThreadStore(hub_env.hub_root)
        store.set_turn_backend_turn_id(managed_turn_id, "opencode-turn-bind")
        resp_after = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")
        assert resp_after.status_code == 200
        after = resp_after.json()

    assert after["stream_available"] is True
    assert after["backend_thread_id"] == "opencode-session-bind"
    assert after["backend_turn_id"] == "opencode-turn-bind"


def test_managed_thread_tail_snapshot_offloads_store_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event_loop_thread_id = threading.get_ident()
    observed_thread_ids: list[tuple[str, int]] = []
    thread = SimpleNamespace(
        agent_id="codex",
        backend_thread_id="backend-thread-1",
    )
    turn = SimpleNamespace(
        execution_id="managed-turn-1",
        status="running",
        started_at="2026-04-06T10:00:00Z",
        finished_at=None,
        backend_id="backend-turn-1",
    )

    class _FakeService:
        def get_thread_target(self, managed_thread_id: str):
            _ = managed_thread_id
            observed_thread_ids.append(("get_thread_target", threading.get_ident()))
            return thread

        def get_running_execution(self, managed_thread_id: str):
            _ = managed_thread_id
            observed_thread_ids.append(("get_running_execution", threading.get_ident()))
            return turn

        def get_latest_execution(self, managed_thread_id: str):
            _ = managed_thread_id
            observed_thread_ids.append(("get_latest_execution", threading.get_ident()))
            return None

    class _FakeTurnStore:
        def __init__(self, hub_root: Path) -> None:
            _ = hub_root
            observed_thread_ids.append(("store_init", threading.get_ident()))

        def get_turn(self, managed_thread_id: str, managed_turn_id: str):
            _ = managed_thread_id, managed_turn_id
            observed_thread_ids.append(("get_turn", threading.get_ident()))
            return None

    def _fake_list_turn_timeline(hub_root: Path, *, execution_id: str):
        _ = hub_root, execution_id
        observed_thread_ids.append(("list_turn_timeline", threading.get_ident()))
        return []

    monkeypatch.setattr(tail_stream, "PmaThreadStore", _FakeTurnStore)
    monkeypatch.setattr(tail_stream, "list_turn_timeline", _fake_list_turn_timeline)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(root=tmp_path),
            )
        )
    )

    import asyncio

    snapshot = asyncio.run(
        tail_stream._build_managed_thread_tail_snapshot(
            request=request,
            service=_FakeService(),
            managed_thread_id="managed-thread-1",
            limit=20,
            level="info",
            since_ms=None,
            resume_after=None,
        )
    )

    assert snapshot["managed_thread_id"] == "managed-thread-1"
    assert snapshot["managed_turn_id"] == "managed-turn-1"
    assert observed_thread_ids
    assert all(
        thread_id != event_loop_thread_id for _label, thread_id in observed_thread_ids
    )


def test_managed_thread_status_aggregates_thread_turn_and_progress(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, managed_turn_id = _seed_managed_thread_with_events(hub_env, app)
    store = PmaThreadStore(hub_env.hub_root)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "status-check"},
                },
            }
        )

    import asyncio

    asyncio.run(_seed())
    store.mark_turn_finished(
        managed_turn_id,
        status="ok",
        assistant_text="completed assistant output",
        backend_turn_id="backend-turn-1",
    )

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["managed_thread_id"] == managed_thread_id
        assert isinstance(payload.get("thread"), dict)
        assert isinstance(payload.get("turn"), dict)
        assert payload["status"] == "completed"
        assert payload["operator_status"] == "reusable"
        assert payload["is_reusable"] is True
        assert payload["status_reason"] == "managed_turn_completed"
        assert payload["status_terminal"] is True
        assert payload["thread"]["lifecycle_status"] == "active"
        assert payload["thread"]["status"] == "completed"
        assert payload["thread"]["normalized_status"] == "completed"
        assert payload["thread"]["operator_status"] == "reusable"
        assert payload["thread"]["is_reusable"] is True
        assert payload["thread"]["accepts_messages"] is True
        assert payload["turn"]["status"] == "ok"
        assert payload["turn"]["phase"] == "completed"
        assert payload["active_turn_diagnostics"]["managed_turn_id"] == managed_turn_id
        assert payload["active_turn_diagnostics"]["request_kind"] == "message"
        assert payload["active_turn_diagnostics"]["prompt_preview"] == "tail prompt"
        assert (
            payload["active_turn_diagnostics"]["backend_thread_id"]
            == "backend-thread-1"
        )
        assert payload["active_turn_diagnostics"]["backend_turn_id"] == "backend-turn-1"
        assert payload["active_turn_diagnostics"]["last_event_type"] == "tool_completed"
        assert "status-check" in (
            payload["active_turn_diagnostics"]["last_event_summary"] or ""
        )
        assert payload["active_turn_diagnostics"]["stalled"] is False
        assert payload["is_alive"] is False
        assert isinstance(payload.get("recent_progress"), list)
        assert payload["latest_turn_status"] == "ok"
        assert payload["latest_assistant_text"] == "completed assistant output"
        assert "completed assistant output" in payload.get("latest_output_excerpt", "")
        assert (
            payload["thread"]["latest_assistant_text"] == "completed assistant output"
        )
        assert payload["thread"]["latest_turn_status"] == "ok"


def test_managed_thread_status_route_offloads_blocking_reads(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)
    original_to_thread = tail_stream.asyncio.to_thread
    offloaded_calls: list[str] = []

    async def _recording_to_thread(func, /, *args, **kwargs):
        offloaded_calls.append(getattr(func, "__name__", repr(func)))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(tail_stream.asyncio, "to_thread", _recording_to_thread)

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

    assert resp.status_code == 200
    assert "build_managed_thread_orchestration_service" in offloaded_calls
    assert "_load_managed_thread_tail_store_state" in offloaded_calls
    assert "_load_managed_thread_status_state" in offloaded_calls


def test_managed_thread_status_surfaces_attention_required_separately_from_failure(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, managed_turn_id = _seed_managed_thread_with_events(hub_env, app)
    store = PmaThreadStore(hub_env.hub_root)
    store.mark_turn_interrupted(managed_turn_id)

    with TestClient(app) as client:
        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "interrupted"
    assert payload["operator_status"] == "reusable"
    assert payload["is_reusable"] is True
    assert payload["status_reason"] == "managed_turn_interrupted"
    assert payload["thread"]["normalized_status"] == "interrupted"
    assert payload["thread"]["operator_status"] == "reusable"
    assert payload["thread"]["is_reusable"] is True
    assert payload["turn"]["phase"] == "interrupted"
    assert payload["latest_assistant_text"] == ""
    assert payload["thread"]["latest_assistant_text"] == ""


def test_refresh_active_turn_diagnostics_recomputes_idle_without_events() -> None:
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    snapshot = {
        "turn_status": "running",
        "started_at": started_at,
        "last_event_at": None,
        "phase": "no_stream_available",
        "guidance": "No stream is available yet.",
        "events": [],
        "active_turn_diagnostics": {
            "managed_turn_id": "managed-turn-1",
            "stalled": False,
            "stall_reason": None,
        },
    }

    refreshed = _refresh_active_turn_diagnostics(snapshot, turn_status="running")

    assert refreshed is not None
    assert refreshed["stalled"] is True
    assert refreshed["stall_reason"] == "no_events_yet"


def test_managed_thread_tail_stream_resumes_with_last_event_id(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, managed_turn_id = _seed_managed_thread_with_events(hub_env, app)
    store = PmaThreadStore(hub_env.hub_root)

    events = app.state.app_server_events

    async def _seed() -> None:
        await events.register_turn("backend-thread-1", "backend-turn-1")
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "first"},
                },
            }
        )
        await events.handle_notification(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "backend-turn-1",
                    "threadId": "backend-thread-1",
                    "item": {"type": "tool", "name": "second"},
                },
            }
        )

    import asyncio

    asyncio.run(_seed())
    store.mark_turn_finished(
        managed_turn_id,
        status="ok",
        assistant_text="done",
        backend_turn_id="backend-turn-1",
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail/events",
            headers={"Last-Event-ID": "1"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: tail" in body
        assert "\nid: 2\n" in body
        assert "\nid: 1\n" not in body


def test_managed_thread_tail_stream_preserves_since_filter_for_live_events(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    managed_thread_id, _ = _seed_managed_thread_with_events(hub_env, app)

    import time

    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 10_000
    new_ms = now_ms

    class FakeEvents:
        async def list_events(
            self,
            thread_id: str,
            turn_id: str,
            *,
            after_id: int = 0,
            limit: int | None = None,
        ):
            _ = thread_id, turn_id, after_id, limit
            return []

        async def stream_entries(
            self,
            thread_id: str,
            turn_id: str,
            *,
            after_id: int = 0,
            heartbeat_interval: float = 15.0,
        ):
            _ = thread_id, turn_id, after_id, heartbeat_interval
            yield {
                "id": 1,
                "received_at": old_ms,
                "message": {
                    "method": "item/completed",
                    "params": {"item": {"type": "tool", "name": "old"}},
                },
            }
            yield {
                "id": 2,
                "received_at": new_ms,
                "message": {
                    "method": "item/completed",
                    "params": {"item": {"type": "tool", "name": "new"}},
                },
            }

    app.state.app_server_events = FakeEvents()

    with TestClient(app) as client:
        resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/tail/events",
            params={"since": "1s"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "event: tail" in body
        assert "\nid: 2\n" in body
        assert "\nid: 1\n" not in body


def test_managed_thread_status_surfaces_zeroclaw_phase_and_last_tool(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    thread = store.create_thread("zeroclaw", hub_env.repo_root.resolve())
    managed_thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="zeroclaw prompt")
    managed_turn_id = str(turn["managed_turn_id"])
    store.set_thread_backend_id(managed_thread_id, "zeroclaw-session-1")
    store.set_turn_backend_turn_id(managed_turn_id, "zeroclaw-turn-1")

    app.state.zeroclaw_supervisor = FakeZeroClawEventSupervisor(
        events=[
            {
                "raw_event": 'event: zeroclaw\ndata: {"message":{"method":"message.delta","params":{"text":"🤔 Thinking..."}}}\n\n',
                "published_at": "2026-03-17T01:00:00Z",
            },
            {
                "raw_event": 'event: zeroclaw\ndata: {"message":{"method":"message.delta","params":{"text":"⏳ web_search"}}}\n\n',
                "published_at": "2026-03-17T01:00:05Z",
            },
        ]
    )

    with TestClient(app) as client:
        status_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()
        assert status_payload["stream_available"] is True
        assert status_payload["turn"]["phase"] == "waiting_on_tool_call"
        assert status_payload["turn"]["last_tool"]["name"] == "web_search"
        assert status_payload["turn"]["last_tool"]["in_flight"] is True
        assert status_payload["active_turn_diagnostics"]["request_kind"] == "message"
        assert (
            status_payload["active_turn_diagnostics"]["last_event_type"]
            == "tool_started"
        )
        assert "web_search" in (
            status_payload["active_turn_diagnostics"]["last_event_summary"] or ""
        )

        tail_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")
        assert tail_resp.status_code == 200
        tail_payload = tail_resp.json()
        assert [event["event_type"] for event in tail_payload["events"]] == [
            "assistant_update",
            "tool_started",
        ]


def test_managed_thread_status_degrades_when_zeroclaw_turn_buffer_is_missing(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    thread = store.create_thread("zeroclaw", hub_env.repo_root.resolve())
    managed_thread_id = str(thread["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="zeroclaw prompt")
    managed_turn_id = str(turn["managed_turn_id"])
    store.set_thread_backend_id(managed_thread_id, "zeroclaw-session-1")
    store.set_turn_backend_turn_id(managed_turn_id, "zeroclaw-turn-1")

    app.state.zeroclaw_supervisor = FakeZeroClawEventSupervisor(
        error=RuntimeError("missing in-memory turn buffer")
    )

    with TestClient(app) as client:
        status_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/status")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()
        assert status_payload["recent_progress"] == []
        assert status_payload["turn"]["last_tool"] is None

        tail_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")
        assert tail_resp.status_code == 200
        tail_payload = tail_resp.json()
        assert tail_payload["events"] == []
