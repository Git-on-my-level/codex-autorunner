from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.agents.opencode.harness import OpenCodeHarness
from codex_autorunner.agents.registry import get_registered_agents
from codex_autorunner.core.sse import SSEEvent


class _StubClient:
    def __init__(self, events: list[SSEEvent]) -> None:
        self._events = events
        self.permission_replies: list[tuple[str, str]] = []
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.question_rejections: list[str] = []
        self.prompt_calls: list[dict[str, object]] = []

    async def stream_events(
        self, *, directory: str | None = None, ready_event: object = None
    ):
        _ = directory
        if ready_event is not None:
            ready_event.set()
        for event in self._events:
            yield event

    async def prompt_async(self, session_id: str, **kwargs: object) -> dict[str, str]:
        self.prompt_calls.append({"session_id": session_id, **kwargs})
        return {}

    async def send_command(self, session_id: str, **kwargs: object) -> dict[str, str]:
        self.prompt_calls.append({"session_id": session_id, **kwargs})
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
        self, client: _StubClient, *, session_stall_timeout_seconds: float | None = None
    ) -> None:
        self._client = client
        self.session_stall_timeout_seconds = session_stall_timeout_seconds

    async def get_client(self, _workspace_root: Path) -> _StubClient:
        return self._client


@pytest.mark.asyncio
async def test_opencode_harness_reports_capabilities_from_contract() -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["opencode"].capabilities
    assert harness.supports("interrupt") is True
    assert harness.supports("review") is True
    assert harness.supports("approvals") is False
    assert report.capabilities == harness.capabilities


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
