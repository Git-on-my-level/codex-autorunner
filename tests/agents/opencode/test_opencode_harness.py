from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_autorunner.agents.opencode import harness as harness_module
from codex_autorunner.agents.opencode.harness import OpenCodeHarness
from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.agents.registry import get_registered_agents
from codex_autorunner.core.orchestration import FreshConversationRequiredError
from codex_autorunner.core.orchestration.runtime_thread_events import (
    merge_runtime_thread_raw_events,
)
from codex_autorunner.core.sse import SSEEvent


class _StubClient:
    def __init__(
        self, events: list[SSEEvent], *, stream_error: Exception | None = None
    ) -> None:
        self._events = events
        self._stream_error = stream_error
        self.permission_replies: list[tuple[str, str]] = []
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.question_rejections: list[str] = []
        self.prompt_calls: list[dict[str, object]] = []
        self.get_session_calls: list[str] = []
        self.get_session_error: Exception | None = None
        self.prompt_error: Exception | None = None
        self.send_command_error: Exception | None = None

    async def stream_events(
        self, *, directory: str | None = None, ready_event: object = None
    ):
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

    async def get_session(self, session_id: str) -> dict[str, str]:
        self.get_session_calls.append(session_id)
        if self.get_session_error is not None:
            raise self.get_session_error
        return {"id": session_id}

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
        self.timeout_workspace_roots: list[Path] = []
        self.runtime_workspace_roots: list[Path] = []

    async def get_client(self, _workspace_root: Path) -> _StubClient:
        return self._client

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
    assert harness.allows_parallel_event_stream() is False
    assert harness.supports("approvals") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_opencode_harness_exposes_backend_runtime_instance_id() -> None:
    workspace_root = Path("/tmp/workspace").resolve()
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
) -> None:
    client = _StubClient([])
    supervisor = _StubSupervisor(client, session_stall_timeout_seconds=17.0)
    harness = OpenCodeHarness(supervisor)
    captured: dict[str, object] = {}

    async def _fake_collect(*args: object, **kwargs: object) -> object:
        captured["stall_timeout_seconds"] = kwargs.get("stall_timeout_seconds")
        return OpenCodeTurnOutput(text="done")

    monkeypatch.setattr(
        harness_module,
        "collect_opencode_output_from_events",
        _fake_collect,
    )

    turn = await harness.start_turn(
        Path("/tmp/workspace"),
        "session-1",
        prompt="hello",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    result = await harness.wait_for_turn(
        Path("/tmp/workspace"),
        "session-1",
        turn.turn_id,
    )

    assert result.status == "ok"
    assert result.assistant_text == "done"
    assert captured["stall_timeout_seconds"] == 17.0
    assert supervisor.timeout_workspace_roots == [Path("/tmp/workspace").resolve()]


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
async def test_opencode_harness_progress_event_stream_reuses_pending_turn_collector() -> (
    None
):
    workspace = Path("/tmp/workspace").resolve()
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
        async for raw_event in harness.progress_event_stream(
            workspace, "session-1", turn.turn_id
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
async def test_opencode_harness_progress_event_stream_accepts_nested_item_session_id() -> (
    None
):
    workspace = Path("/tmp/workspace").resolve()
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
        async for raw_event in harness.progress_event_stream(
            workspace, "session-1", turn.turn_id
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
async def test_opencode_harness_logs_skipped_progress_events_without_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Path("/tmp/workspace").resolve()
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
    assert any(
        event["event"] == "opencode.progress_event.skipped"
        and event["reason"] == "missing_session_id"
        and event["method"] == "item/reasoning/summaryTextDelta"
        for event in logged_events
    )


@pytest.mark.asyncio
async def test_opencode_harness_progress_event_stream_replays_buffer_before_live_events() -> (
    None
):
    workspace = Path("/tmp/workspace").resolve()
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
    pending.progress_event_history.append({"message": {"method": "first"}})

    streamed: list[Any] = []

    async def _collect_stream() -> None:
        async for raw_event in harness.progress_event_stream(
            workspace, "session-1", turn.turn_id
        ):
            streamed.append(raw_event)

    stream_task = asyncio.create_task(_collect_stream())
    await asyncio.sleep(0)

    for queue in list(pending.progress_event_subscribers):
        queue.put_nowait({"message": {"method": "second"}})
        queue.put_nowait(None)

    await stream_task

    assert streamed == [
        {"message": {"method": "first"}},
        {"message": {"method": "second"}},
    ]


@pytest.mark.asyncio
async def test_opencode_harness_progress_event_stream_is_empty_after_pending_turn_cleanup() -> (
    None
):
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))

    streamed = [
        raw_event
        async for raw_event in harness.progress_event_stream(
            Path("/tmp/workspace").resolve(), "session-1", "turn-1"
        )
    ]

    assert streamed == []


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
async def test_opencode_harness_repo_scoped_turn_rejects_out_of_workspace_permission() -> (
    None
):
    workspace = Path("/tmp/workspace").resolve()
    client = _StubClient(
        [
            SSEEvent(
                event="permission.asked",
                data=(
                    '{"sessionID":"session-1","properties":{"id":"perm-1",'
                    '"permission":"external_directory","patterns":["/tmp/elsewhere/*"],'
                    '"metadata":{"filepath":"/tmp/elsewhere/file.py"}}}'
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
async def test_opencode_harness_repo_scoped_turn_allows_in_workspace_permission() -> (
    None
):
    workspace = Path("/tmp/workspace").resolve()
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
async def test_opencode_harness_noninteractive_turn_rejects_questions() -> None:
    workspace = Path("/tmp/workspace").resolve()
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
