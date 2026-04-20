from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_autorunner.agents.base import harness_progress_event_stream
from codex_autorunner.agents.opencode import harness as harness_module
from codex_autorunner.agents.opencode import (
    progress_synthesis as progress_synthesis_module,
)
from codex_autorunner.agents.opencode.harness import (
    OpenCodeHarness,
)
from codex_autorunner.agents.opencode.protocol_payload import (
    normalize_message_text,
    parse_message_response,
)
from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.agents.registry import get_registered_agents
from codex_autorunner.core.orchestration import FreshConversationRequiredError
from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    merge_runtime_thread_raw_events,
    normalize_runtime_thread_raw_event,
)
from codex_autorunner.core.sse import SSEEvent


class _StubClient:
    def __init__(
        self, events: list[SSEEvent], *, stream_error: Exception | None = None
    ) -> None:
        self._events = events
        self._stream_error = stream_error
        self.stream_calls: list[dict[str, object]] = []
        self.permission_replies: list[tuple[str, str]] = []
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.question_rejections: list[str] = []
        self.prompt_calls: list[dict[str, object]] = []
        self.get_session_calls: list[str] = []
        self.session_responses: dict[str, Any] = {}
        self.list_messages_calls: list[str] = []
        self.messages_response: Any = []
        self.list_messages_error: Exception | None = None
        self.get_session_error: Exception | None = None
        self.prompt_error: Exception | None = None
        self.send_command_error: Exception | None = None

    async def stream_events(
        self,
        *,
        directory: str | None = None,
        ready_event: object = None,
        session_id: str | None = None,
        paths: object = None,
    ):
        self.stream_calls.append(
            {
                "directory": directory,
                "ready_event": ready_event,
                "session_id": session_id,
                "paths": paths,
            }
        )
        _ = directory
        if ready_event is not None:
            ready_event.set()
        for event in self._events:
            yield event
        if self._stream_error is not None:
            raise self._stream_error

    async def prompt_async(self, session_id: str, **kwargs: object) -> dict[str, str]:
        self.prompt_calls.append({"session_id": session_id, **kwargs})
        if self.prompt_error is not None:
            raise self.prompt_error
        return {}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        self.get_session_calls.append(session_id)
        if self.get_session_error is not None:
            raise self.get_session_error
        response = self.session_responses.get(session_id)
        if isinstance(response, dict):
            return response
        return {"id": session_id}

    async def list_messages(self, session_id: str, **_kwargs: Any) -> Any:
        self.list_messages_calls.append(session_id)
        if self.list_messages_error is not None:
            raise self.list_messages_error
        return self.messages_response

    async def send_command(self, session_id: str, **kwargs: object) -> dict[str, str]:
        self.prompt_calls.append({"session_id": session_id, **kwargs})
        if self.send_command_error is not None:
            raise self.send_command_error
        return {}

    async def session_status(
        self, *, directory: str | None = None
    ) -> dict[str, object]:
        _ = directory
        return {}

    async def providers(self, *, directory: str | None = None) -> dict[str, object]:
        _ = directory
        return {}

    async def respond_permission(self, *, request_id: str, reply: str) -> None:
        self.permission_replies.append((request_id, reply))

    async def reply_question(
        self, request_id: str, *, answers: list[list[str]]
    ) -> None:
        self.question_replies.append((request_id, answers))

    async def reject_question(self, request_id: str) -> None:
        self.question_rejections.append(request_id)


class _StubSupervisor:
    def __init__(
        self,
        client: _StubClient,
        *,
        session_stall_timeout_seconds: float | None = None,
        runtime_instance_id: str | None = None,
    ) -> None:
        self._client = client
        self.session_stall_timeout_seconds = session_stall_timeout_seconds
        self.runtime_instance_id = runtime_instance_id
        self.get_client_error: Exception | None = None
        self.timeout_workspace_roots: list[Path] = []
        self.runtime_workspace_roots: list[Path] = []
        self.started_workspace_roots: list[Path] = []
        self.finished_workspace_roots: list[Path] = []

    async def get_client(self, _workspace_root: Path) -> _StubClient:
        if self.get_client_error is not None:
            raise self.get_client_error
        return self._client

    async def get_client_for_turn(self, workspace_root: Path) -> _StubClient:
        self.started_workspace_roots.append(workspace_root)
        return self._client

    async def mark_turn_started(self, workspace_root: Path) -> None:
        self.started_workspace_roots.append(workspace_root)

    async def mark_turn_finished(self, workspace_root: Path) -> None:
        self.finished_workspace_roots.append(workspace_root)

    async def session_stall_timeout_seconds_for_workspace(
        self, workspace_root: Path
    ) -> float | None:
        self.timeout_workspace_roots.append(workspace_root)
        return self.session_stall_timeout_seconds

    async def backend_runtime_instance_id_for_workspace(
        self, workspace_root: Path
    ) -> str | None:
        self.runtime_workspace_roots.append(workspace_root)
        return self.runtime_instance_id


@pytest.mark.asyncio
async def test_opencode_harness_reports_capabilities_from_contract() -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["opencode"].capabilities
    assert harness.supports("interrupt") is True
    assert harness.supports("review") is True
    assert harness.supports("event_streaming") is True
    assert harness.allows_parallel_event_stream() is True
    assert harness.supports("approvals") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_opencode_harness_stream_events_warns_when_no_pending_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))
    with caplog.at_level(logging.WARNING, logger=harness_module._logger.name):
        events = [e async for e in harness.stream_events(Path("."), "conv-1", "turn-1")]
    assert events == []
    assert any(
        "stream_events: no pending turn" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_opencode_harness_stream_events_falls_back_on_turn_id_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))
    from codex_autorunner.agents.opencode.harness import _PendingTurnConfig

    config = _PendingTurnConfig(
        model_payload=None,
        approval_mode=None,
        sandbox_policy=None,
        question_policy="ignore",
    )
    harness._pending_turns[("conv-1", "real-turn-id")] = config
    await config.event_buffer.append({"test": "event"})
    await config.event_buffer.close()
    with caplog.at_level(logging.WARNING, logger=harness_module._logger.name):
        events = [
            e async for e in harness.stream_events(Path("."), "conv-1", "wrong-turn-id")
        ]
    assert len(events) == 1
    assert events[0]["test"] == "event"
    assert any("turn_id mismatch" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_opencode_harness_list_progress_events_warns_when_no_pending_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))
    with caplog.at_level(logging.WARNING, logger=harness_module._logger.name):
        got = await harness.list_progress_events("conv-1", "turn-1")
    assert got == []
    assert any(
        "list_progress_events: no pending turn" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_opencode_harness_exposes_backend_runtime_instance_id(
    tmp_path: Path,
) -> None:
    workspace_root = (tmp_path / "workspace").resolve()
    supervisor = _StubSupervisor(
        _StubClient([]),
        runtime_instance_id=" opencode:scope=workspace:pid=4242 ",
    )
    harness = OpenCodeHarness(supervisor)

    runtime_instance_id = await harness.backend_runtime_instance_id(workspace_root)

    assert runtime_instance_id == "opencode:scope=workspace:pid=4242"
    assert supervisor.runtime_workspace_roots == [workspace_root]


@pytest.mark.asyncio
async def test_opencode_harness_backend_runtime_instance_id_is_optional() -> None:
    class _SupervisorWithoutRuntimeId:
        async def get_client(self, _workspace_root: Path) -> _StubClient:
            return _StubClient([])

        async def session_stall_timeout_seconds_for_workspace(
            self, workspace_root: Path
        ) -> float | None:
            _ = workspace_root
            return None

    harness = OpenCodeHarness(_SupervisorWithoutRuntimeId())

    assert await harness.backend_runtime_instance_id(Path(".")) is None


@pytest.mark.asyncio
async def test_opencode_harness_resume_conversation_requires_fresh_binding_for_missing_session() -> (
    None
):
    client = _StubClient([])
    request = httpx.Request("GET", "http://127.0.0.1:4096/session/session-1")
    response = httpx.Response(404, request=request)
    client.get_session_error = httpx.HTTPStatusError(
        "missing session",
        request=request,
        response=response,
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    with pytest.raises(FreshConversationRequiredError) as exc_info:
        await harness.resume_conversation(Path("."), "session-1")

    assert exc_info.value.conversation_id == "session-1"
    assert exc_info.value.operation == "resume_conversation"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_opencode_harness_start_turn_requires_fresh_binding_for_invalid_session() -> (
    None
):
    client = _StubClient([])
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:4096/session/session-1/prompt_async",
    )
    response = httpx.Response(400, request=request)
    client.prompt_error = httpx.HTTPStatusError(
        "invalid session",
        request=request,
        response=response,
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    with pytest.raises(FreshConversationRequiredError) as exc_info:
        await harness.start_turn(
            Path("."),
            "session-1",
            prompt="hello",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )

    assert exc_info.value.conversation_id == "session-1"
    assert exc_info.value.operation == "start_turn"
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_opencode_harness_start_review_requires_fresh_binding_for_invalid_session() -> (
    None
):
    client = _StubClient([])
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:4096/session/session-1/command",
    )
    response = httpx.Response(400, request=request)
    client.send_command_error = httpx.HTTPStatusError(
        "invalid session",
        request=request,
        response=response,
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    with pytest.raises(FreshConversationRequiredError) as exc_info:
        await harness.start_review(
            Path("."),
            "session-1",
            prompt="review this",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )

    assert exc_info.value.conversation_id == "session-1"
    assert exc_info.value.operation == "start_review"
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_opencode_harness_keeps_turn_guard_through_resume_start_and_wait(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="message.completed",
                data='{"sessionID":"session-1","info":{"id":"assistant-1","role":"assistant"},"parts":[{"type":"text","text":"hello world"}]}',
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    supervisor = _StubSupervisor(client)
    harness = OpenCodeHarness(supervisor)

    resumed = await harness.resume_conversation(workspace, "session-1")

    assert resumed.id == "session-1"
    assert supervisor.started_workspace_roots == [workspace]
    assert supervisor.finished_workspace_roots == []

    turn = await harness.start_turn(
        workspace,
        resumed.id,
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    assert supervisor.started_workspace_roots == [workspace]
    assert supervisor.finished_workspace_roots == []

    result = await harness.wait_for_turn(workspace, resumed.id, turn.turn_id)

    assert result.status == "ok"
    assert result.assistant_text == "hello world"
    assert supervisor.finished_workspace_roots == [workspace]


@pytest.mark.asyncio
async def test_opencode_harness_start_turn_uses_backend_turn_id_from_prompt_response(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient([])

    async def _prompt_async(session_id: str, **kwargs: object) -> dict[str, object]:
        return {
            "info": {"id": "backend-turn-1"},
            "sessionID": session_id,
            **kwargs,
        }

    client.prompt_async = _prompt_async  # type: ignore[method-assign]
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    assert turn.turn_id == "backend-turn-1"
    assert ("session-1", "backend-turn-1") in harness._pending_turns


@pytest.mark.asyncio
async def test_opencode_harness_start_review_uses_backend_turn_id_from_command_response(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient([])

    async def _send_command(session_id: str, **kwargs: object) -> dict[str, object]:
        return {
            "id": "backend-review-1",
            "sessionID": session_id,
            **kwargs,
        }

    client.send_command = _send_command  # type: ignore[method-assign]
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_review(
        workspace,
        "session-1",
        prompt="review this",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    assert turn.turn_id == "backend-review-1"
    assert ("session-1", "backend-review-1") in harness._pending_turns


@pytest.mark.asyncio
async def test_opencode_harness_start_turn_returns_before_prompt_finishes_and_synthesizes_events(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient([])
    release_prompt = asyncio.Event()

    async def _prompt_async(session_id: str, **kwargs: object) -> dict[str, object]:
        _ = kwargs
        await release_prompt.wait()
        return {
            "info": {"id": "backend-turn-1"},
            "sessionID": session_id,
            "parts": [{"type": "text", "text": "Agent reply"}],
        }

    client.prompt_async = _prompt_async  # type: ignore[method-assign]
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    assert turn.turn_id.startswith("session-1:")
    pending = harness._pending_turns[("session-1", turn.turn_id)]
    assert pending.command_task is not None
    assert not pending.command_task.done()

    event_iter = harness.stream_events(workspace, "session-1", turn.turn_id)
    first_event = await asyncio.wait_for(event_iter.__anext__(), timeout=1.0)
    await event_iter.aclose()

    assert first_event["message"]["method"] == "session.status"
    assert first_event["message"]["params"]["status"]["type"] == "busy"

    release_prompt.set()
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert result.assistant_text == "Agent reply"
    methods = [
        event.get("message", {}).get("method")
        for event in result.raw_events
        if isinstance(event, dict)
    ]
    assert "message.completed" in methods
    assert "session.idle" in methods


@pytest.mark.asyncio
async def test_opencode_harness_polls_messages_for_rich_progress_when_sse_is_silent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient([])
    release_prompt = asyncio.Event()

    async def _prompt_async(session_id: str, **kwargs: object) -> dict[str, object]:
        _ = kwargs
        await release_prompt.wait()
        return {
            "info": {"id": "backend-turn-1"},
            "sessionID": session_id,
            "parts": [{"type": "text", "text": "Agent reply"}],
        }

    message_snapshots = [
        {
            "data": [
                {
                    "info": {"id": "assistant-1", "role": "assistant"},
                    "parts": [],
                }
            ]
        },
        {
            "data": [
                {
                    "info": {"id": "assistant-1", "role": "assistant"},
                    "parts": [
                        {
                            "id": "reason-1",
                            "type": "reasoning",
                            "messageID": "assistant-1",
                            "sessionID": "session-1",
                            "text": "Looking for AGENTS.md",
                        },
                        {
                            "id": "tool-1",
                            "type": "tool",
                            "messageID": "assistant-1",
                            "sessionID": "session-1",
                            "tool": "shell",
                            "input": "rg AGENTS.md",
                            "state": {"status": "running"},
                        },
                    ],
                }
            ]
        },
    ]
    list_message_calls = 0

    async def _list_messages(session_id: str, **_kwargs: Any) -> Any:
        nonlocal list_message_calls
        assert session_id == "session-1"
        snapshot = message_snapshots[
            min(list_message_calls, len(message_snapshots) - 1)
        ]
        list_message_calls += 1
        return snapshot

    client.prompt_async = _prompt_async  # type: ignore[method-assign]
    client.list_messages = _list_messages  # type: ignore[method-assign]
    harness = OpenCodeHarness(_StubSupervisor(client))
    monkeypatch.setattr(
        progress_synthesis_module, "SILENT_TURN_PROGRESS_POLL_SECONDS", 0.01
    )

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    event_iter = harness.stream_events(workspace, "session-1", turn.turn_id)
    collected: list[dict[str, Any]] = []
    for _ in range(10):
        event = await asyncio.wait_for(event_iter.__anext__(), timeout=1.0)
        collected.append(event)
        part_types = {
            item.get("message", {})
            .get("params", {})
            .get("properties", {})
            .get("part", {})
            .get("type")
            for item in collected
            if item.get("message", {}).get("method") == "message.part.updated"
        }
        if {"reasoning", "tool"}.issubset(part_types):
            break
    await event_iter.aclose()

    message_part_events = [
        item
        for item in collected
        if item.get("message", {}).get("method") == "message.part.updated"
    ]
    assert message_part_events
    assert any(
        item["message"]["params"]["properties"]["part"]["type"] == "reasoning"
        for item in message_part_events
    )
    assert any(
        item["message"]["params"]["properties"]["part"]["type"] == "tool"
        for item in message_part_events
    )

    normalized_state = RuntimeThreadRunEventState()
    normalized_events = []
    for item in collected:
        normalized_events.extend(
            await normalize_runtime_thread_raw_event(item, normalized_state)
        )
    assert any(getattr(item, "kind", None) == "thinking" for item in normalized_events)
    assert any(
        getattr(item, "tool_name", None) == "shell" for item in normalized_events
    )

    release_prompt.set()
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    assert result.status == "ok"
    assert result.assistant_text == "Agent reply"


@pytest.mark.asyncio
async def test_opencode_harness_polls_messages_when_preconnected_stream_only_has_keepalives_and_busy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(event="server.connected", data='{"type":"server.connected"}'),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"busy"}}}',
            ),
        ]
    )
    release_prompt = asyncio.Event()

    async def _prompt_async(session_id: str, **kwargs: object) -> dict[str, object]:
        _ = kwargs
        await release_prompt.wait()
        return {
            "info": {"id": "backend-turn-1"},
            "sessionID": session_id,
            "parts": [{"type": "text", "text": "Agent reply"}],
        }

    message_snapshots = [
        {
            "data": [
                {
                    "info": {"id": "assistant-1", "role": "assistant"},
                    "parts": [
                        {
                            "id": "reason-1",
                            "type": "reasoning",
                            "messageID": "assistant-1",
                            "sessionID": "session-1",
                            "text": "Checking stream fallbacks",
                        },
                        {
                            "id": "tool-1",
                            "type": "tool",
                            "messageID": "assistant-1",
                            "sessionID": "session-1",
                            "tool": "shell",
                            "input": "rg stream_events",
                            "state": {"status": "running"},
                        },
                    ],
                }
            ]
        }
    ]
    list_message_calls = 0

    async def _list_messages(session_id: str, **_kwargs: Any) -> Any:
        nonlocal list_message_calls
        assert session_id == "session-1"
        list_message_calls += 1
        return message_snapshots[0]

    client.prompt_async = _prompt_async  # type: ignore[method-assign]
    client.list_messages = _list_messages  # type: ignore[method-assign]
    harness = OpenCodeHarness(_StubSupervisor(client))
    monkeypatch.setattr(
        progress_synthesis_module, "SILENT_TURN_PROGRESS_POLL_SECONDS", 0.01
    )

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    event_iter = harness.stream_events(workspace, "session-1", turn.turn_id)
    collected: list[dict[str, Any]] = []
    for _ in range(10):
        event = await asyncio.wait_for(event_iter.__anext__(), timeout=1.0)
        collected.append(event)
        part_types = {
            item.get("message", {})
            .get("params", {})
            .get("properties", {})
            .get("part", {})
            .get("type")
            for item in collected
            if item.get("message", {}).get("method") == "message.part.updated"
        }
        if {"reasoning", "tool"}.issubset(part_types):
            break
    await event_iter.aclose()

    assert list_message_calls >= 1
    message_part_events = [
        item
        for item in collected
        if item.get("message", {}).get("method") == "message.part.updated"
    ]
    assert any(
        item["message"]["params"]["properties"]["part"]["type"] == "reasoning"
        for item in message_part_events
    )
    assert any(
        item["message"]["params"]["properties"]["part"]["type"] == "tool"
        for item in message_part_events
    )

    release_prompt.set()
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    assert result.status == "ok"
    assert result.assistant_text == "Agent reply"


@pytest.mark.asyncio
async def test_opencode_harness_releases_reserved_turn_when_start_turn_setup_fails(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    supervisor = _StubSupervisor(_StubClient([]))
    harness = OpenCodeHarness(supervisor)

    resumed = await harness.resume_conversation(workspace, "session-1")
    supervisor.get_client_error = RuntimeError("startup failed")

    with pytest.raises(RuntimeError, match="startup failed"):
        await harness.start_turn(
            workspace,
            resumed.id,
            prompt="hello",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )

    assert supervisor.started_workspace_roots == [workspace]
    assert supervisor.finished_workspace_roots == [workspace]


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_collects_plain_text_output() -> None:
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.delta",
                        data='{"sessionID":"session-1","delta":"hello "}',
                    ),
                    SSEEvent(
                        event="message.completed",
                        data='{"sessionID":"session-1","text":"hello world"}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "hello world"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_resolves_stall_timeout_per_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _StubClient([])
    supervisor = _StubSupervisor(client, session_stall_timeout_seconds=17.0)
    harness = OpenCodeHarness(supervisor)
    workspace = (tmp_path / "workspace").resolve()
    captured: dict[str, object] = {}

    async def _fake_collect(*args: object, **kwargs: object) -> object:
        captured["stall_timeout_seconds"] = kwargs.get("stall_timeout_seconds")
        captured["first_event_timeout_seconds"] = kwargs.get(
            "first_event_timeout_seconds"
        )
        return OpenCodeTurnOutput(text="done")

    monkeypatch.setattr(
        harness_module,
        "collect_opencode_output_from_events",
        _fake_collect,
    )

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    result = await harness.wait_for_turn(
        workspace,
        "session-1",
        turn.turn_id,
    )

    assert result.status == "ok"
    assert result.assistant_text == "done"
    assert captured["stall_timeout_seconds"] == 17.0
    assert captured["first_event_timeout_seconds"] == 17.0
    assert supervisor.timeout_workspace_roots == [workspace]


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_prefers_phase_marked_final_answer() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.completed",
                        data='{"sessionID":"session-1","text":"draft reply","phase":"commentary"}',
                    ),
                    SSEEvent(
                        event="message.completed",
                        data='{"sessionID":"session-1","text":"final reply","phase":"final_answer"}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "final reply"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_uses_stream_output_over_commentary_only_completion() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.completed",
                        data='{"sessionID":"session-1","text":"draft reply","phase":"commentary"}',
                    ),
                    SSEEvent(
                        event="item/agentMessage/delta",
                        data='{"sessionID":"session-1","itemId":"item-1","delta":"final reply"}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "final reply"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_progress_event_stream_reuses_pending_turn_collector(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="message.completed",
                data='{"sessionID":"session-1","info":{"id":"assistant-1","role":"assistant"},"parts":[{"type":"text","text":"hello world"}]}',
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    streamed: list[Any] = []

    async def _collect_stream() -> None:
        async for raw_event in harness_progress_event_stream(
            harness, workspace, "session-1", turn.turn_id
        ):
            streamed.append(raw_event)

    stream_task = asyncio.create_task(_collect_stream())
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    await stream_task

    assert result.status == "ok"
    assert result.assistant_text == "hello world"
    assert len(streamed) == 1
    assert streamed[0] == result.raw_events[0]
    assert merge_runtime_thread_raw_events(streamed, result.raw_events) == list(
        result.raw_events
    )


@pytest.mark.asyncio
async def test_opencode_harness_progress_event_stream_accepts_nested_item_session_id(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="item/agentMessage/delta",
                data='{"item":{"sessionID":"session-1"},"itemId":"item-1","delta":"hello world"}',
            ),
            SSEEvent(
                event="message.completed",
                data='{"sessionID":"session-1","text":"hello world"}',
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    streamed: list[Any] = []

    async def _collect_stream() -> None:
        async for raw_event in harness_progress_event_stream(
            harness, workspace, "session-1", turn.turn_id
        ):
            streamed.append(raw_event)

    stream_task = asyncio.create_task(_collect_stream())
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    await stream_task

    assert result.status == "ok"
    assert "item/agentMessage/delta" in [
        event["message"]["method"] for event in streamed
    ]


@pytest.mark.asyncio
async def test_opencode_harness_publishes_progress_events_without_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="item/reasoning/summaryTextDelta",
                data='{"item":{"type":"reasoning"},"delta":"thinking"}',
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))
    logged_events: list[dict[str, Any]] = []

    def _capture_log_event(
        logger: Any,
        level: int,
        event: str,
        **fields: Any,
    ) -> None:
        _ = logger
        logged_events.append({"level": level, "event": event, **fields})

    monkeypatch.setattr(harness_module, "log_event", _capture_log_event)

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert not any(
        event["event"] == "opencode.progress_event.skipped"
        and event["reason"] == "missing_session_id"
        and event["method"] == "item/reasoning/summaryTextDelta"
        for event in logged_events
    )


@pytest.mark.asyncio
async def test_opencode_harness_progress_event_stream_replays_buffer_before_live_events(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )
    pending = harness._pending_turns[("session-1", turn.turn_id)]
    await pending.event_buffer.append({"message": {"method": "first"}})

    streamed: list[Any] = []

    async def _collect_stream() -> None:
        async for raw_event in harness_progress_event_stream(
            harness, workspace, "session-1", turn.turn_id
        ):
            streamed.append(raw_event)

    async def _inject_second_and_close() -> None:
        await asyncio.sleep(0)
        await pending.event_buffer.append({"message": {"method": "second"}})
        await pending.event_buffer.close()

    stream_task = asyncio.create_task(_collect_stream())
    await asyncio.create_task(_inject_second_and_close())

    await stream_task

    assert streamed == [
        {"message": {"method": "first"}},
        {"message": {"method": "second"}},
    ]


@pytest.mark.asyncio
async def test_opencode_harness_progress_event_stream_is_empty_after_pending_turn_cleanup(
    tmp_path: Path,
) -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))
    workspace = (tmp_path / "workspace").resolve()

    streamed = [
        raw_event
        async for raw_event in harness_progress_event_stream(
            harness,
            workspace,
            "session-1",
            "turn-1",
        )
    ]

    assert streamed == []


@pytest.mark.asyncio
async def test_opencode_harness_list_progress_events_empty_without_pending() -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))
    assert await harness.list_progress_events("session-1", "turn-1") == []


@pytest.mark.asyncio
async def test_opencode_harness_list_progress_events_returns_buffered_copy(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))
    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )
    pending = harness._pending_turns[("session-1", turn.turn_id)]
    await pending.event_buffer.append({"message": {"method": "tool"}})

    events = await harness.list_progress_events("session-1", turn.turn_id)
    assert events == [{"message": {"method": "tool"}}]
    events.append({"other": True})
    assert await harness.list_progress_events("session-1", turn.turn_id) == [
        {"message": {"method": "tool"}}
    ]


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_uses_session_scoped_event_stream(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()

    class _SessionScopedClient(_StubClient):
        async def stream_events(
            self,
            *,
            directory: str | None = None,
            ready_event: object = None,
            session_id: str | None = None,
            paths: object = None,
        ):
            self.stream_calls.append(
                {
                    "directory": directory,
                    "ready_event": ready_event,
                    "session_id": session_id,
                    "paths": paths,
                }
            )
            if ready_event is not None:
                ready_event.set()
            if session_id != "session-1":
                return
            for event in self._events:
                yield event

    client = _SessionScopedClient(
        [
            SSEEvent(
                event="message.part.updated",
                data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"OK"}}}',
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert result.assistant_text == "OK"
    assert any(call["session_id"] == "session-1" for call in client.stream_calls)


@pytest.mark.asyncio
async def test_opencode_harness_stream_events_uses_session_scoped_event_stream(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="message.part.updated",
                data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"OK"}}}',
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    streamed: list[Any] = []

    async def _tail() -> None:
        async for event in harness.stream_events(workspace, "session-1", turn.turn_id):
            streamed.append(event)

    tail_task = asyncio.create_task(_tail())
    await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    await tail_task

    assert streamed
    assert any(call["session_id"] == "session-1" for call in client.stream_calls)
    assert streamed[0]["message"]["method"] == "message.part.updated"


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_recovers_late_disconnect_after_completion() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.completed",
                        data='{"sessionID":"session-1","text":"final reply","phase":"final_answer"}',
                    ),
                ],
                stream_error=RuntimeError("stream dropped"),
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "final reply"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_still_raises_without_terminal_completion() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.delta",
                        data='{"sessionID":"session-1","delta":"partial"}',
                    ),
                ],
                stream_error=RuntimeError("stream dropped"),
            )
        )
    )

    with pytest.raises(RuntimeError, match="stream dropped"):
        await harness.wait_for_turn(Path("."), "session-1", "turn-1")


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_no_pending_list_messages_failure_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No-pending path: list_messages recovery must not fail the turn on RPC errors."""
    client = _StubClient(
        [
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ],
    )
    client.list_messages_error = httpx.ConnectError("transport failed", request=None)
    harness = OpenCodeHarness(_StubSupervisor(client))

    with caplog.at_level(logging.WARNING, logger=harness_module._logger.name):
        result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == ""
    assert result.errors == []
    assert client.list_messages_calls == ["session-1"]
    assert any(
        "wait_for_turn.list_messages_failed" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_reports_errors() -> None:
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="turn/error",
                        data='{"sessionID":"session-1","message":"stream failed"}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "error"
    assert result.assistant_text == ""
    assert result.errors == ["stream failed"]


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_collects_message_part_updates() -> None:
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"OK"}}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "OK"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_collects_message_part_deltas_with_direct_ids() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.updated",
                        data='{"sessionID":"session-1","properties":{"info":{"id":"assistant-1","role":"assistant"}}}',
                    ),
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"id":"part-1","messageID":"assistant-1","type":"text","text":"Hello"},"delta":"Hello"}}',
                    ),
                    SSEEvent(
                        event="message.part.delta",
                        data='{"sessionID":"session-1","properties":{"partID":"part-1","messageID":"assistant-1","delta":" world"}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "Hello world"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_merges_cumulative_message_part_updates() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"Hello"}}}',
                    ),
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"Hello world"}}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "Hello world"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_does_not_treat_reasoning_part_deltas_as_output() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"id":"reason-1","type":"reasoning"},"delta":"thinking"}}',
                    ),
                    SSEEvent(
                        event="message.part.delta",
                        data='{"sessionID":"session-1","properties":{"partID":"reason-1","delta":" more"}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == ""
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_ignores_user_message_part_updates() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","messageID":"user-1","text":"user prompt"}}}',
                    ),
                    SSEEvent(
                        event="message.updated",
                        data='{"sessionID":"session-1","properties":{"info":{"id":"user-1","role":"user"}}}',
                    ),
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","messageID":"assistant-1","text":"assistant reply"}}}',
                    ),
                    SSEEvent(
                        event="message.updated",
                        data='{"sessionID":"session-1","properties":{"info":{"id":"assistant-1","role":"assistant"}}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "assistant reply"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_recovers_late_disconnect_after_idle() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"final reply"},"delta":"final reply"}}',
                    ),
                    SSEEvent(
                        event="session.idle",
                        data='{"sessionID":"session-1"}',
                    ),
                ],
                stream_error=RuntimeError("stream dropped"),
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "final reply"


@pytest.mark.asyncio
async def test_opencode_harness_repo_scoped_turn_rejects_out_of_workspace_permission(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    outside_dir = str(workspace.parent / "elsewhere")
    outside_file = str(workspace.parent / "elsewhere" / "file.py")
    client = _StubClient(
        [
            SSEEvent(
                event="permission.asked",
                data=(
                    '{"sessionID":"session-1","properties":{"id":"perm-1",'
                    '"permission":"external_directory","patterns":["'
                    + outside_dir
                    + '/*"],'
                    '"metadata":{"filepath":"' + outside_file + '"}}}'
                ),
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode="on-request",
        sandbox_policy="workspaceWrite",
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert client.permission_replies == [("perm-1", "reject")]


@pytest.mark.asyncio
async def test_opencode_harness_repo_scoped_turn_allows_in_workspace_permission(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="permission.asked",
                data=(
                    '{"sessionID":"session-1","properties":{"id":"perm-1",'
                    '"permission":"external_directory","patterns":["src/*"],'
                    '"metadata":{"filepath":"src/app.py"}}}'
                ),
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode="on-request",
        sandbox_policy="workspaceWrite",
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert client.permission_replies == [("perm-1", "once")]


@pytest.mark.asyncio
async def test_opencode_harness_noninteractive_turn_rejects_questions(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="question.asked",
                data=(
                    '{"sessionID":"session-1","properties":{"id":"q-1","questions":'
                    '[{"text":"Continue?","options":[{"label":"Yes"},{"label":"No"}]}]}}'
                ),
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode="on-request",
        sandbox_policy="workspaceWrite",
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert client.question_rejections == ["q-1"]


@pytest.mark.asyncio
async def test_opencode_harness_stream_events_yields_sessionless_events(
    tmp_path: Path,
) -> None:
    """stream_events() should yield events that lack a sessionID instead of
    dropping them."""
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="item/agentMessage/delta",
                data='{"delta":"partial content"}',
            ),
            SSEEvent(
                event="session.idle",
                data='{"sessionID":"session-1"}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))
    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )
    collected: list[Any] = []

    async def _tail() -> None:
        async for chunk in harness.stream_events(workspace, "session-1", turn.turn_id):
            collected.append(chunk)

    tail_task = asyncio.create_task(_tail())
    await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    await tail_task

    assert len(collected) == 1
    assert collected[0]["message"]["method"] == "item/agentMessage/delta"


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_recovers_sessionless_roleless_text(
    tmp_path: Path,
) -> None:
    """wait_for_turn() should recover final text from a sessionless,
    roleless message.completed when the text differs from the prompt."""
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="message.completed",
                data='{"info":{"id":"m1"},'
                '"parts":[{"type":"text","text":"Agent reply"}]}',
            ),
            SSEEvent(
                event="session.idle",
                data='{"sessionID":"session-1"}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))
    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="Do something",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)
    assert result.status == "ok"
    assert result.assistant_text == "Agent reply"


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_recovers_final_text_from_session_messages(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="session.idle",
                data='{"sessionID":"session-1"}',
            ),
        ]
    )
    client.messages_response = [
        {
            "info": {"id": "user-1", "role": "user"},
            "parts": [{"type": "text", "text": "Can you echo hello world?"}],
        },
        {
            "info": {"id": "assistant-1", "role": "assistant"},
            "parts": [{"type": "text", "text": "hello world"}],
        },
    ]
    harness = OpenCodeHarness(_StubSupervisor(client))
    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="Can you echo hello world?",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert result.assistant_text == "hello world"
    assert client.list_messages_calls == ["session-1"]


@pytest.mark.asyncio
async def test_opencode_harness_managed_turn_auto_approves_child_session_permission(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    _tests_diff = str(workspace.parent / "tests_diff.txt")
    client = _StubClient(
        [
            SSEEvent(
                event="permission.asked",
                data=(
                    '{"sessionID":"child-1","properties":{"id":"perm-1",'
                    '"permission":"external_directory","patterns":["'
                    + _tests_diff
                    + '"],'
                    '"metadata":{"filepath":"' + _tests_diff + '"}}}'
                ),
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    client.session_responses["child-1"] = {
        "id": "child-1",
        "parentID": "session-1",
    }
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode="never",
        sandbox_policy="dangerFullAccess",
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert client.permission_replies == [("perm-1", "once")]
    assert client.get_session_calls == ["child-1"]


@pytest.mark.asyncio
async def test_opencode_harness_rewrites_child_text_parts_into_accumulated_thinking(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="session.created",
                data='{"sessionID":"child-1","info":{"id":"child-1","parentID":"session-1"}}',
            ),
            SSEEvent(
                event="message.part.updated",
                data=(
                    '{"sessionID":"child-1","properties":{"messageID":"assistant-child-1",'
                    '"info":{"id":"assistant-child-1","role":"assistant"},'
                    '"delta":{"text":"Current version is v1.9.10."},'
                    '"part":{"id":"child-part-1","messageID":"assistant-child-1",'
                    '"type":"text","text":"Current version is v1.9.10."}}}'
                ),
            ),
            SSEEvent(
                event="message.part.updated",
                data=(
                    '{"sessionID":"child-1","properties":{"messageID":"assistant-child-1",'
                    '"info":{"id":"assistant-child-1","role":"assistant"},'
                    '"delta":{"text":" A patch release would be v1.9.11."},'
                    '"part":{"id":"child-part-1","messageID":"assistant-child-1",'
                    '"type":"text","text":"Current version is v1.9.10. A patch release would be v1.9.11."}}}'
                ),
            ),
            SSEEvent(
                event="message.completed",
                data=(
                    '{"sessionID":"session-1","info":{"id":"assistant-parent-1",'
                    '"role":"assistant"},"parts":[{"type":"text",'
                    '"text":"Patch release v1.9.11 is tagged and pushed to origin/main."}]}'
                ),
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="Do a patch release from origin main",
        model=None,
        reasoning=None,
        approval_mode="never",
        sandbox_policy="dangerFullAccess",
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert (
        result.assistant_text
        == "Patch release v1.9.11 is tagged and pushed to origin/main."
    )

    normalized_state = RuntimeThreadRunEventState()
    normalized_events = []
    for raw_event in result.raw_events:
        normalized_events.extend(
            await normalize_runtime_thread_raw_event(raw_event, normalized_state)
        )

    thinking_messages = [
        getattr(event, "message", "")
        for event in normalized_events
        if getattr(event, "kind", None) == "thinking"
    ]
    assert thinking_messages == [
        "Current version is v1.9.10.",
        "Current version is v1.9.10. A patch release would be v1.9.11.",
    ]

    assistant_messages = [
        getattr(event, "content", "")
        for event in normalized_events
        if getattr(event, "delta_type", None)
        in {"assistant_message", "assistant_stream"}
    ]
    assert assistant_messages == [
        "Patch release v1.9.11 is tagged and pushed to origin/main."
    ]


@pytest.mark.asyncio
async def test_opencode_harness_rewrites_child_agent_messages_without_polluting_final_output(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="session.created",
                data='{"sessionID":"child-1","info":{"id":"child-1","parentID":"session-1"}}',
            ),
            SSEEvent(
                event="item/agentMessage/delta",
                data='{"sessionID":"child-1","itemId":"child-msg-1","delta":"Now let me bump the version"}',
            ),
            SSEEvent(
                event="item/completed",
                data=(
                    '{"sessionID":"child-1","item":{"type":"agentMessage","id":"child-msg-1",'
                    '"text":"Now let me bump the version and create the release commit."}}'
                ),
            ),
            SSEEvent(
                event="message.completed",
                data=(
                    '{"sessionID":"session-1","info":{"id":"assistant-parent-1",'
                    '"role":"assistant"},"parts":[{"type":"text",'
                    '"text":"Done. Patch release v1.9.11 is tagged and pushed to origin/main."}]}'
                ),
            ),
            SSEEvent(
                event="session.status",
                data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
            ),
        ]
    )
    harness = OpenCodeHarness(_StubSupervisor(client))

    turn = await harness.start_turn(
        workspace,
        "session-1",
        prompt="Do a patch release from origin main",
        model=None,
        reasoning=None,
        approval_mode="never",
        sandbox_policy="dangerFullAccess",
    )
    result = await harness.wait_for_turn(workspace, "session-1", turn.turn_id)

    assert result.status == "ok"
    assert (
        result.assistant_text
        == "Done. Patch release v1.9.11 is tagged and pushed to origin/main."
    )

    normalized_state = RuntimeThreadRunEventState()
    normalized_events = []
    for raw_event in result.raw_events:
        normalized_events.extend(
            await normalize_runtime_thread_raw_event(raw_event, normalized_state)
        )

    thinking_messages = [
        getattr(event, "message", "")
        for event in normalized_events
        if getattr(event, "kind", None) == "thinking"
    ]
    assert thinking_messages == [
        "Now let me bump the version",
        "Now let me bump the version and create the release commit.",
    ]

    assistant_messages = [
        getattr(event, "content", "")
        for event in normalized_events
        if getattr(event, "delta_type", None)
        in {"assistant_message", "assistant_stream"}
    ]
    assert assistant_messages == [
        "Done. Patch release v1.9.11 is tagged and pushed to origin/main."
    ]


def test_normalize_message_text_filters_reasoning_parts() -> None:
    """normalize_message_text should exclude non-text parts from content lists."""
    content = [
        {"type": "reasoning", "text": "thinking hard"},
        {"type": "text", "text": "the answer"},
    ]
    result = normalize_message_text(content)
    assert result == "the answer"


def test_normalize_message_text_includes_untyped_parts() -> None:
    """Parts without a type field should be included (legacy/fallback)."""
    content = [
        {"text": "legacy text"},
        {"type": "text", "text": " and modern text"},
    ]
    result = normalize_message_text(content)
    assert result == "legacy text and modern text"


def test_normalize_message_text_excludes_output_text_type() -> None:
    """output_text is not 'text' and should be excluded for consistency
    with the runtime event normalizer."""
    content = [
        {"type": "output_text", "text": "output only"},
        {"type": "text", "text": "actual text"},
    ]
    result = normalize_message_text(content)
    assert result == "actual text"


def test_collect_terminal_text_filters_reasoning_from_completed_message() -> None:
    """parse_message_response should exclude reasoning parts from completed messages."""
    payload = {
        "parts": [
            {"type": "reasoning", "text": "thinking about the answer"},
            {"type": "text", "text": "the answer is 42"},
        ],
    }
    result = parse_message_response(payload)
    assert result.text == "the answer is 42"
