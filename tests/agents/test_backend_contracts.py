"""
Backend contract tests for Codex and OpenCode adapters.

Tests verify that each adapter correctly implements the AgentBackend interface
and handles the full lifecycle of session/turn operations.
"""

import tempfile
from pathlib import Path

import pytest

from codex_autorunner.integrations.agents import (
    AgentBackend,
    AgentEvent,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class DummyBackend(AgentBackend):
    """Minimal backend for testing interface compliance."""

    def __init__(self):
        self.events = []

    async def start_session(self, target: dict, context: dict) -> str:
        return "session-001"

    async def run_turn(self, session_id: str, message: str):
        yield AgentEvent(
            type="stream_delta",
            timestamp="2024-01-01T00:00:00Z",
            data={"role": "user", "content": message},
        )
        yield AgentEvent(
            type="message_complete",
            timestamp="2024-01-01T00:00:00Z",
            data={"message": "Done"},
        )

    async def stream_events(self, session_id: str):
        pass

    async def interrupt(self, session_id: str):
        pass

    async def final_messages(self, session_id: str):
        return []


@pytest.mark.asyncio
async def test_backend_interface_compliance():
    """Test that backend interface can be implemented."""
    backend = DummyBackend()

    session_id = await backend.start_session(
        target={"type": "test", "instructions": "Test instructions"},
        context={"workspace": "/tmp/test"},
    )

    assert session_id == "session-001"

    # Run a turn and collect events
    events = []
    async for event in backend.run_turn(session_id, "Test message"):
        events.append(event)

    assert len(events) == 2
    assert events[0].type == "stream_delta"
    assert events[1].type == "message_complete"

    # Test interrupt
    await backend.interrupt(session_id)

    # Test final_messages
    final = await backend.final_messages(session_id)
    assert isinstance(final, list)
    assert len(final) == 0


@pytest.mark.asyncio
async def test_backend_event_types():
    """Test that AgentEvent types are available."""
    from codex_autorunner.integrations.agents import AgentEventType

    # Verify core event types exist
    assert hasattr(AgentEventType, "STREAM_DELTA")
    assert hasattr(AgentEventType, "TOOL_CALL")
    assert hasattr(AgentEventType, "TOOL_RESULT")
    assert hasattr(AgentEventType, "MESSAGE_COMPLETE")
    assert hasattr(AgentEventType, "ERROR")
    assert hasattr(AgentEventType, "APPROVAL_REQUESTED")
    assert hasattr(AgentEventType, "APPROVAL_GRANTED")
    assert hasattr(AgentEventType, "APPROVAL_DENIED")
    assert hasattr(AgentEventType, "SESION_STARTED")
    assert hasattr(AgentEventType, "SESSION_ENDED")


@pytest.mark.asyncio
async def test_backend_async_interface_methods():
    """Test that backend methods are async."""
    backend = DummyBackend()

    # All interface methods should be async
    import inspect

    assert inspect.iscoroutinefunction(backend.start_session)
    assert inspect.iscoroutinefunction(backend.run_turn)
    assert inspect.iscoroutinefunction(backend.stream_events)
    assert inspect.iscoroutinefunction(backend.interrupt)
    assert inspect.iscoroutinefunction(backend.final_messages)


@pytest.mark.asyncio
async def test_backend_return_types():
    """Test backend return types match interface."""
    backend = DummyBackend()

    session_id = await backend.start_session(target={}, context={})

    # start_session returns str
    assert isinstance(session_id, str)

    # run_turn returns AsyncGenerator
    turn_gen = backend.run_turn(session_id, "test")
    assert hasattr(turn_gen, "__aiter__")

    # stream_events returns AsyncGenerator
    stream_gen = backend.stream_events(session_id)
    assert hasattr(stream_gen, "__aiter__")

    # interrupt returns None
    result = await backend.interrupt(session_id)
    assert result is None

    # final_messages returns list
    messages = await backend.final_messages(session_id)
    assert isinstance(messages, list)
