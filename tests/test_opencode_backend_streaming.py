from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.agents.opencode.events import SSEEvent
from codex_autorunner.core.ports.run_event import Completed, OutputDelta
from codex_autorunner.integrations.agents.opencode_backend import OpenCodeBackend


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
async def test_opencode_streaming_coalesces_text_deltas(tmp_path: Path) -> None:
    session_id = "s-test"
    deltas = ["Hello ", "world", " from ", "test", "."]
    events = [
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-test","properties":{"delta":{"text":"Hello "},'
            '"part":{"type":"text","text":"Hello "}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-test","properties":{"delta":{"text":"world"},'
            '"part":{"type":"text","text":"Hello world"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-test","properties":{"delta":{"text":" from "},'
            '"part":{"type":"text","text":"Hello world from "}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-test","properties":{"delta":{"text":"test"},'
            '"part":{"type":"text","text":"Hello world from test"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s-test","properties":{"delta":{"text":"."},'
            '"part":{"type":"text","text":"Hello world from test."}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s-test"}'),
    ]

    backend = OpenCodeBackend(workspace_root=tmp_path, supervisor=None)
    backend._client = _FakeOpenCodeClient(events)

    assistant_chunks: list[str] = []
    final_message: Optional[str] = None

    async for event in backend.run_turn_events(session_id, "Ping"):
        if isinstance(event, OutputDelta) and event.delta_type == "assistant_stream":
            assistant_chunks.append(event.content)
        if isinstance(event, Completed):
            final_message = event.final_message

    assert final_message == "".join(deltas)
    assert "".join(assistant_chunks) == "".join(deltas)
    # Regression: avoid emitting one OutputDelta per tiny delta
    assert len(assistant_chunks) <= len(deltas) // 2
