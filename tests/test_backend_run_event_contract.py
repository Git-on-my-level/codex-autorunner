from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_autorunner.agents.opencode.events import SSEEvent
from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPES,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    is_terminal_run_event,
)
from codex_autorunner.integrations.agents.codex_backend import CodexAppServerBackend
from codex_autorunner.integrations.agents.opencode_backend import OpenCodeBackend


def _fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / name


def _assert_turn_contract(events: list[object]) -> None:
    assert events, "expected at least one event"
    assert isinstance(events[0], Started), "first event must be Started"
    assert is_terminal_run_event(events[-1]), "last event must be terminal"
    terminal_events = [event for event in events if is_terminal_run_event(event)]
    assert len(terminal_events) == 1, "expected exactly one terminal event"
    for event in events:
        if isinstance(event, OutputDelta):
            assert event.delta_type in RUN_EVENT_DELTA_TYPES


@pytest.mark.asyncio
async def test_codex_backend_run_turn_events_obeys_contract(tmp_path: Path) -> None:
    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=tmp_path,
    )
    backend._thread_id = "thread-123"

    with patch.object(
        backend, "_ensure_client", new_callable=AsyncMock
    ) as ensure_client:
        client = MagicMock()
        handle = MagicMock()
        handle.turn_id = "turn-123"
        handle.wait = AsyncMock(
            return_value=MagicMock(
                final_message="Done", agent_messages=[], raw_events=[]
            )
        )
        client.turn_start = AsyncMock(return_value=handle)
        ensure_client.return_value = client

        events = [
            event
            async for event in backend.run_turn_events("thread-123", "hello codex")
        ]

    _assert_turn_contract(events)
    assert isinstance(events[-1], Completed)
    assert events[-1].final_message == "Done"


def test_codex_notification_parser_golden_transcript() -> None:
    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=Path("."),
    )
    notifications = json.loads(
        _fixture_path("codex_run_event_notifications_golden.json").read_text(
            encoding="utf-8"
        )
    )
    events = [
        event
        for event in (
            backend._map_to_run_event(notification) for notification in notifications
        )
        if event is not None
    ]

    assert [type(event).__name__ for event in events] == [
        "OutputDelta",
        "OutputDelta",
        "TokenUsage",
        "Failed",
    ]
    assert (
        "".join(event.content for event in events if isinstance(event, OutputDelta))
        == "Debugging"
    )
    usage = [event for event in events if isinstance(event, TokenUsage)]
    assert len(usage) == 1
    assert usage[0].usage["total_tokens"] == 20
    assert isinstance(events[-1], Failed)
    assert "permission denied" in events[-1].error_message


def test_codex_notification_parser_supports_outputdelta_reasoning_and_item_completed() -> (
    None
):
    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=Path("."),
    )
    notifications = [
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"turnId": "turn-1", "delta": "planning next step"},
        },
        {
            "method": "outputDelta",
            "params": {"turnId": "turn-1", "delta": "hello"},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": " there"},
        },
        {
            "method": "item/commandExecution/outputDelta",
            "params": {"turnId": "turn-1", "itemId": "cmd-1", "delta": "ls output"},
        },
        {
            "method": "item/fileChange/outputDelta",
            "params": {"turnId": "turn-1", "itemId": "file-1", "delta": "patch line"},
        },
        {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "command": ["ls", "-la"]},
            },
        },
        {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "done"},
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-1",
                "tokenUsage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    events = [
        event
        for event in (
            backend._map_to_run_event(notification) for notification in notifications
        )
        if event is not None
    ]

    assert isinstance(events[0], RunNotice)
    assert events[0].kind == "thinking"
    assert "planning" in events[0].message

    assert isinstance(events[1], OutputDelta)
    assert events[1].content == "hello"
    assert events[1].delta_type == "assistant_stream"

    assert isinstance(events[2], OutputDelta)
    assert events[2].content == " there"
    assert events[2].delta_type == "assistant_stream"

    assert isinstance(events[3], OutputDelta)
    assert events[3].content == "ls output"
    assert events[3].delta_type == "log_line"

    assert isinstance(events[4], OutputDelta)
    assert events[4].content == "patch line"
    assert events[4].delta_type == "log_line"

    assert isinstance(events[5], ToolCall)
    assert events[5].tool_name == "ls -la"

    assert isinstance(events[6], OutputDelta)
    assert events[6].content == "done"

    assert isinstance(events[7], TokenUsage)
    assert events[7].usage["input_tokens"] == 10


def test_codex_notification_parser_accumulates_reasoning_deltas_per_item() -> None:
    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=Path("."),
    )
    notifications = [
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"turnId": "turn-1", "itemId": "reason-1", "delta": "plan"},
        },
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"turnId": "turn-1", "itemId": "reason-1", "delta": " next"},
        },
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"turnId": "turn-1", "itemId": "reason-1", "delta": " step"},
        },
    ]

    events = [
        event
        for event in (
            backend._map_to_run_event(notification) for notification in notifications
        )
        if event is not None
    ]

    assert [type(event).__name__ for event in events] == [
        "RunNotice",
        "RunNotice",
        "RunNotice",
    ]
    assert all(isinstance(event, RunNotice) for event in events)
    assert events[0].message == "plan"
    assert events[1].message == "plan next"
    assert events[2].message == "plan next step"


def test_opencode_sse_parser_golden_transcript() -> None:
    backend = OpenCodeBackend(workspace_root=Path("."), supervisor=None)
    payloads = json.loads(
        _fixture_path("opencode_run_event_sse_golden.json").read_text(encoding="utf-8")
    )
    sse_events = [
        SSEEvent(event="message", data=json.dumps(payload, ensure_ascii=True))
        for payload in payloads
    ]
    events: list[object] = []
    for sse in sse_events:
        events.extend(backend._convert_sse_to_run_event(sse))

    assert [type(event).__name__ for event in events] == [
        "OutputDelta",
        "OutputDelta",
        "TokenUsage",
        "Failed",
    ]
    assert (
        "".join(event.content for event in events if isinstance(event, OutputDelta))
        == "Debugging"
    )
    usage = [event for event in events if isinstance(event, TokenUsage)]
    assert len(usage) == 1
    assert usage[0].usage["total_tokens"] == 20
    assert isinstance(events[-1], Failed)
    assert "permission denied" in events[-1].error_message


class _FakeOpenCodeClient:
    def __init__(self, events: list[SSEEvent]):
        self._events = events

    async def stream_events(self, *, directory=None, ready_event=None, paths=None):
        if ready_event is not None:
            ready_event.set()
        for event in self._events:
            yield event

    async def session_status(self, directory=None):
        return {"status": {"type": "idle"}}

    async def providers(self, directory=None):
        return {}

    async def respond_permission(self, request_id: str, reply: str):
        return None

    async def reply_question(self, request_id: str, answers):
        return None

    async def reject_question(self, request_id: str):
        return None

    async def prompt_async(self, *args, **kwargs):
        return {}

    async def send_command(self, *args, **kwargs):
        return None


@pytest.mark.anyio
async def test_opencode_backend_run_turn_events_obeys_contract(tmp_path: Path) -> None:
    session_id = "s-contract"
    sse_events = [
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-contract","properties":{"delta":{"text":"Debug"},'
            '"part":{"type":"text","text":"Debug"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-contract","properties":{"delta":{"text":"ging"},'
            '"part":{"type":"text","text":"Debugging"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s-contract"}'),
    ]

    backend = OpenCodeBackend(workspace_root=tmp_path, supervisor=None)
    backend._client = _FakeOpenCodeClient(sse_events)

    events = [event async for event in backend.run_turn_events(session_id, "hello")]

    _assert_turn_contract(events)
    assert isinstance(events[-1], Completed)
    assert events[-1].final_message == "Debugging"
