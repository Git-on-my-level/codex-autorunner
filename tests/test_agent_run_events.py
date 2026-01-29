from typing import Any, Union

import pytest

from codex_autorunner.core.ports import AgentBackend
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    Started,
    ToolCall,
    now_iso,
)


class MockAgentBackend(AgentBackend):
    def __init__(self):
        self._session_id = "test-session-123"

    async def start_session(self, target: dict, context: dict) -> str:
        return self._session_id

    async def run_turn(self, session_id: str, message: str) -> Any:
        yield OutputDelta(
            timestamp="2024-01-01T00:00:00Z", content=message, delta_type="user_message"
        )
        yield OutputDelta(
            timestamp="2024-01-01T00:00:01Z",
            content="Hello",
            delta_type="assistant_stream",
        )
        yield Completed(timestamp="2024-01-01T00:00:02Z", final_message="Hello")

    async def stream_events(self, session_id: str) -> Any:
        yield OutputDelta(
            timestamp="2024-01-01T00:00:00Z",
            content="Stream content",
            delta_type="assistant_stream",
        )

    async def run_turn_events(self, session_id: str, message: str) -> Any:
        ts = now_iso()
        yield Started(timestamp=ts, session_id=session_id or self._session_id)
        yield OutputDelta(timestamp=ts, content=message, delta_type="user_message")
        yield OutputDelta(
            timestamp=ts, content="Response", delta_type="assistant_stream"
        )
        yield Completed(timestamp=ts, final_message="Response")

    async def interrupt(self, session_id: str) -> None:
        pass

    async def final_messages(self, session_id: str) -> list[str]:
        return []

    async def request_approval(
        self, description: str, context: Union[dict[str, Any], None] = None
    ) -> bool:
        return True


class TestRunEventTypes:
    def test_started_event(self):
        event = Started(timestamp="2024-01-01T00:00:00Z", session_id="session-123")
        assert event.timestamp == "2024-01-01T00:00:00Z"
        assert event.session_id == "session-123"

    def test_output_delta_event(self):
        event = OutputDelta(
            timestamp="2024-01-01T00:00:00Z",
            content="Hello world",
            delta_type="text",
        )
        assert event.content == "Hello world"
        assert event.delta_type == "text"

    def test_tool_call_event(self):
        event = ToolCall(
            timestamp="2024-01-01T00:00:00Z",
            tool_name="bash",
            tool_input={"command": "ls"},
        )
        assert event.tool_name == "bash"
        assert event.tool_input == {"command": "ls"}

    def test_approval_requested_event(self):
        event = ApprovalRequested(
            timestamp="2024-01-01T00:00:00Z",
            request_id="req-123",
            description="Execute command",
            context={"tool": "bash"},
        )
        assert event.request_id == "req-123"
        assert event.description == "Execute command"
        assert event.context == {"tool": "bash"}

    def test_completed_event(self):
        event = Completed(
            timestamp="2024-01-01T00:00:00Z",
            final_message="Task completed",
        )
        assert event.final_message == "Task completed"

    def test_failed_event(self):
        event = Failed(
            timestamp="2024-01-01T00:00:00Z",
            error_message="Connection failed",
        )
        assert event.error_message == "Connection failed"


class TestHappyPathEventSequence:
    @pytest.mark.asyncio
    async def test_happy_path_sequence(self):
        backend = MockAgentBackend()
        session_id = "test-session"
        message = "Hello"

        events = []
        async for event in backend.run_turn_events(session_id, message):
            events.append(event)

        assert len(events) >= 4
        assert isinstance(events[0], Started)
        assert events[0].session_id == session_id

        user_messages = [
            e
            for e in events
            if isinstance(e, OutputDelta) and e.delta_type == "user_message"
        ]
        assert len(user_messages) == 1
        assert user_messages[0].content == message

        assistant_messages = [
            e
            for e in events
            if isinstance(e, OutputDelta) and e.delta_type == "assistant_stream"
        ]
        assert len(assistant_messages) == 1
        assert assistant_messages[0].content == "Response"

        completed = [e for e in events if isinstance(e, Completed)]
        assert len(completed) == 1
        assert completed[0].final_message == "Response"


class TestFailureEventSequence:
    @pytest.mark.asyncio
    async def test_failure_sequence(self):
        class FailingBackend(AgentBackend):
            def __init__(self):
                self._session_id = "test-session-123"

            async def start_session(self, target: dict, context: dict) -> str:
                return self._session_id

            async def run_turn(self, session_id: str, message: str) -> Any:
                yield OutputDelta(
                    timestamp="2024-01-01T00:00:00Z",
                    content=message,
                    delta_type="user_message",
                )
                yield Failed(
                    timestamp="2024-01-01T00:00:01Z",
                    error_message="Something went wrong",
                )

            async def stream_events(self, session_id: str) -> Any:
                pass

            async def run_turn_events(self, session_id: str, message: str) -> Any:
                ts = now_iso()
                yield Started(timestamp=ts, session_id=session_id or self._session_id)
                yield OutputDelta(
                    timestamp=ts, content=message, delta_type="user_message"
                )
                yield Failed(timestamp=ts, error_message="Simulated failure")

            async def interrupt(self, session_id: str) -> None:
                pass

            async def final_messages(self, session_id: str) -> list[str]:
                return []

            async def request_approval(
                self, description: str, context: Union[dict[str, Any], None] = None
            ) -> bool:
                return False

        backend = FailingBackend()
        session_id = "test-session"
        message = "Test message"

        events = []
        async for event in backend.run_turn_events(session_id, message):
            events.append(event)

        assert len(events) >= 3
        assert isinstance(events[0], Started)

        user_messages = [
            e
            for e in events
            if isinstance(e, OutputDelta) and e.delta_type == "user_message"
        ]
        assert len(user_messages) == 1
        assert user_messages[0].content == message

        failed = [e for e in events if isinstance(e, Failed)]
        assert len(failed) == 1
        assert failed[0].error_message == "Simulated failure"

        completed = [e for e in events if isinstance(e, Completed)]
        assert len(completed) == 0


class TestToolCallEventSequence:
    @pytest.mark.asyncio
    async def test_tool_call_sequence(self):
        class ToolCallBackend(AgentBackend):
            def __init__(self):
                self._session_id = "test-session-123"

            async def start_session(self, target: dict, context: dict) -> str:
                return self._session_id

            async def run_turn(self, session_id: str, message: str) -> Any:
                pass

            async def stream_events(self, session_id: str) -> Any:
                pass

            async def run_turn_events(self, session_id: str, message: str) -> Any:
                ts = now_iso()
                yield Started(timestamp=ts, session_id=session_id or self._session_id)
                yield OutputDelta(
                    timestamp=ts, content=message, delta_type="user_message"
                )
                yield OutputDelta(
                    timestamp=ts, content="Thinking...", delta_type="assistant_stream"
                )
                yield ToolCall(
                    timestamp=ts,
                    tool_name="bash",
                    tool_input={"command": "ls -la"},
                )
                yield OutputDelta(
                    timestamp=ts, content="Done", delta_type="assistant_stream"
                )
                yield Completed(timestamp=ts, final_message="Done")

            async def interrupt(self, session_id: str) -> None:
                pass

            async def final_messages(self, session_id: str) -> list[str]:
                return []

            async def request_approval(
                self, description: str, context: Union[dict[str, Any], None] = None
            ) -> bool:
                return True

        backend = ToolCallBackend()
        session_id = "test-session"
        message = "List files"

        events = []
        async for event in backend.run_turn_events(session_id, message):
            events.append(event)

        assert len(events) >= 5
        assert isinstance(events[0], Started)

        tool_calls = [e for e in events if isinstance(e, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].tool_name == "bash"
        assert tool_calls[0].tool_input == {"command": "ls -la"}

        completed = [e for e in events if isinstance(e, Completed)]
        assert len(completed) == 1


class TestApprovalEventSequence:
    @pytest.mark.asyncio
    async def test_approval_sequence(self):
        class ApprovalBackend(AgentBackend):
            def __init__(self):
                self._session_id = "test-session-123"

            async def start_session(self, target: dict, context: dict) -> str:
                return self._session_id

            async def run_turn(self, session_id: str, message: str) -> Any:
                pass

            async def stream_events(self, session_id: str) -> Any:
                pass

            async def run_turn_events(self, session_id: str, message: str) -> Any:
                ts = now_iso()
                yield Started(timestamp=ts, session_id=session_id or self._session_id)
                yield OutputDelta(
                    timestamp=ts, content=message, delta_type="user_message"
                )
                yield ApprovalRequested(
                    timestamp=ts,
                    request_id="req-123",
                    description="Execute bash command",
                    context={"tool": "bash", "command": "rm -rf /"},
                )
                yield OutputDelta(
                    timestamp=ts, content="Approved", delta_type="assistant_stream"
                )
                yield Completed(timestamp=ts, final_message="Approved")

            async def interrupt(self, session_id: str) -> None:
                pass

            async def final_messages(self, session_id: str) -> list[str]:
                return []

            async def request_approval(
                self, description: str, context: Union[dict[str, Any], None] = None
            ) -> bool:
                return True

        backend = ApprovalBackend()
        session_id = "test-session"
        message = "Delete files"

        events = []
        async for event in backend.run_turn_events(session_id, message):
            events.append(event)

        assert len(events) >= 4
        assert isinstance(events[0], Started)

        approvals = [e for e in events if isinstance(e, ApprovalRequested)]
        assert len(approvals) == 1
        assert approvals[0].request_id == "req-123"
        assert approvals[0].description == "Execute bash command"
        assert approvals[0].context == {"tool": "bash", "command": "rm -rf /"}

        completed = [e for e in events if isinstance(e, Completed)]
        assert len(completed) == 1
