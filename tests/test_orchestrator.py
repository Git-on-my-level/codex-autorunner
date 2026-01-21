from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.agents.orchestrator import (
    CodexOrchestrator,
    OpenCodeOrchestrator,
    TurnStatus,
)
from codex_autorunner.core.app_server_events import AppServerEventBuffer


@pytest.fixture()
def mock_codex_harness():
    harness = MagicMock()
    harness.new_conversation = AsyncMock()
    harness.resume_conversation = AsyncMock()
    harness.start_turn = AsyncMock()
    harness.start_review = AsyncMock()
    harness.interrupt = AsyncMock()
    harness.stream_events = AsyncMock()
    return harness


@pytest.fixture()
def mock_opencode_harness():
    harness = MagicMock()
    harness.new_conversation = AsyncMock()
    harness.resume_conversation = AsyncMock()
    harness.start_turn = AsyncMock()
    harness.start_review = AsyncMock()
    harness.interrupt = AsyncMock()
    harness.stream_events = AsyncMock()
    harness._supervisor = MagicMock()
    harness._supervisor.get_client = AsyncMock()
    return harness


@pytest.fixture()
def mock_events():
    events = MagicMock(spec=AppServerEventBuffer)
    return events


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


class TestCodexOrchestrator:
    def test_init(self, mock_codex_harness, mock_events):
        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        assert orchestrator._harness is mock_codex_harness
        assert orchestrator._events is mock_events

    @pytest.mark.anyio
    async def test_create_conversation_new(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import ConversationRef

        expected_ref = ConversationRef(agent="codex", id="new-conv-123")
        mock_codex_harness.new_conversation.return_value = expected_ref

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.create_or_resume_conversation(
            workspace_root, "agent-1", title="Test Conversation"
        )

        assert result == expected_ref
        mock_codex_harness.new_conversation.assert_called_once_with(
            workspace_root, "Test Conversation"
        )
        mock_codex_harness.resume_conversation.assert_not_called()

    @pytest.mark.anyio
    async def test_create_conversation_resume(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import ConversationRef

        expected_ref = ConversationRef(agent="codex", id="existing-conv-456")
        mock_codex_harness.resume_conversation.return_value = expected_ref

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.create_or_resume_conversation(
            workspace_root, "agent-1", conversation_id="existing-conv-456"
        )

        assert result == expected_ref
        mock_codex_harness.resume_conversation.assert_called_once_with(
            workspace_root, "existing-conv-456"
        )
        mock_codex_harness.new_conversation.assert_not_called()

    @pytest.mark.anyio
    async def test_run_turn_success(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_turn.return_value = turn_ref

        async def stream_events_gen(*args):
            yield '{"message":{"params":{"output":"Hello "}}}'
            yield '{"message":{"params":{"output":"world"}}}'
            yield '{"other":"data"}'

        mock_codex_harness.stream_events = stream_events_gen

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.run_turn(
            workspace_root,
            "conv-1",
            "test prompt",
            model="gpt-4",
            reasoning="minimal",
            approval_mode="allow",
            sandbox_policy=None,
            timeout_seconds=60.0,
        )

        assert result["turn_id"] == "turn-1"
        assert result["conversation_id"] == "conv-1"
        assert result["status"] == TurnStatus.COMPLETED
        assert result["output"] == "Hello \nworld"

        mock_codex_harness.start_turn.assert_called_once_with(
            workspace_root,
            "conv-1",
            "test prompt",
            "gpt-4",
            "minimal",
            approval_mode="allow",
            sandbox_policy=None,
        )

    @pytest.mark.anyio
    async def test_run_turn_handles_malformed_json_silently(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_turn.return_value = turn_ref

        async def stream_events_gen(*args):
            yield '{"message":{"params":{"output":"Valid output"}}}'
            yield "invalid json"
            yield '{"invalid":}'
            yield '{"message":{"no_params":"data"}}'
            yield '{"message":{"params":{"output":"Another valid output"}}}'

        mock_codex_harness.stream_events = stream_events_gen

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.run_turn(workspace_root, "conv-1", "test prompt")

        assert result["turn_id"] == "turn-1"
        assert result["status"] == TurnStatus.COMPLETED
        assert result["output"] == "Valid output\nAnother valid output"

    @pytest.mark.anyio
    async def test_run_turn_empty_events(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_turn.return_value = turn_ref

        async def stream_events_gen(*args):
            return
            yield

        mock_codex_harness.stream_events = stream_events_gen

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.run_turn(workspace_root, "conv-1", "test prompt")

        assert result["turn_id"] == "turn-1"
        assert result["status"] == TurnStatus.COMPLETED
        assert result["output"] == ""

    @pytest.mark.anyio
    async def test_run_turn_nested_params_not_dict(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_turn.return_value = turn_ref

        async def stream_events_gen(*args):
            yield '{"message":{"params":"not a dict"}}'
            yield '{"message":{"params":{"output":"Valid output"}}}'

        mock_codex_harness.stream_events = stream_events_gen

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.run_turn(workspace_root, "conv-1", "test prompt")

        assert result["turn_id"] == "turn-1"
        assert result["status"] == TurnStatus.COMPLETED
        assert result["output"] == "Valid output"

    @pytest.mark.anyio
    async def test_stream_turn_events(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_turn.return_value = turn_ref

        async def stream_events_gen(*args):
            yield '{"event":"data"}'
            yield '{"event":"more"}'

        mock_codex_harness.stream_events = stream_events_gen

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        events = []
        async for event in orchestrator.stream_turn_events(
            workspace_root, "conv-1", "test prompt"
        ):
            events.append(event)

        assert len(events) == 4
        assert events[0]["type"] == "turn_started"
        assert events[0]["data"]["turn_id"] == "turn-1"
        assert events[1]["type"] == "event"
        assert events[1]["data"] == '{"event":"data"}'
        assert events[2]["type"] == "event"
        assert events[2]["data"] == '{"event":"more"}'
        assert events[3]["type"] == "turn_completed"
        assert events[3]["data"]["turn_id"] == "turn-1"

    @pytest.mark.anyio
    async def test_interrupt_turn(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.interrupt_turn(
            workspace_root, "conv-1", turn_id="turn-1", grace_seconds=10.0
        )

        assert result is True
        mock_codex_harness.interrupt.assert_called_once_with(
            workspace_root, "conv-1", "turn-1"
        )

    @pytest.mark.anyio
    async def test_interrupt_turn_no_turn_id(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.interrupt_turn(
            workspace_root, "conv-1", grace_seconds=30.0
        )

        assert result is True
        mock_codex_harness.interrupt.assert_called_once_with(
            workspace_root, "conv-1", None
        )

    @pytest.mark.anyio
    async def test_start_review(self, mock_codex_harness, mock_events, workspace_root):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_review.return_value = turn_ref

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.start_review(
            workspace_root,
            "conv-1",
            prompt="Review this",
            model="gpt-4",
            reasoning="low",
            approval_mode="allow",
            sandbox_policy=None,
            timeout_seconds=30.0,
        )

        assert result == turn_ref
        mock_codex_harness.start_review.assert_called_once_with(
            workspace_root,
            "conv-1",
            "Review this",
            "gpt-4",
            "low",
            approval_mode="allow",
            sandbox_policy=None,
        )

    @pytest.mark.anyio
    async def test_start_review_no_prompt(
        self, mock_codex_harness, mock_events, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-1", turn_id="turn-1")
        mock_codex_harness.start_review.return_value = turn_ref

        orchestrator = CodexOrchestrator(mock_codex_harness, mock_events)
        result = await orchestrator.start_review(workspace_root, "conv-1")

        assert result == turn_ref
        mock_codex_harness.start_review.assert_called_once_with(
            workspace_root,
            "conv-1",
            "",
            None,
            None,
            approval_mode=None,
            sandbox_policy=None,
        )


class TestOpenCodeOrchestrator:
    def test_init(self, mock_opencode_harness):
        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        assert orchestrator._harness is mock_opencode_harness

    @pytest.mark.anyio
    async def test_create_conversation_new(self, mock_opencode_harness, workspace_root):
        from codex_autorunner.agents.types import ConversationRef

        expected_ref = ConversationRef(agent="opencode", id="new-conv-789")
        mock_opencode_harness.new_conversation.return_value = expected_ref

        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        result = await orchestrator.create_or_resume_conversation(
            workspace_root, "agent-2", title="Test Conversation"
        )

        assert result == expected_ref
        mock_opencode_harness.new_conversation.assert_called_once_with(
            workspace_root, "Test Conversation"
        )
        mock_opencode_harness.resume_conversation.assert_not_called()

    @pytest.mark.anyio
    async def test_create_conversation_resume(
        self, mock_opencode_harness, workspace_root
    ):
        from codex_autorunner.agents.types import ConversationRef

        expected_ref = ConversationRef(agent="opencode", id="existing-conv-999")
        mock_opencode_harness.resume_conversation.return_value = expected_ref

        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        result = await orchestrator.create_or_resume_conversation(
            workspace_root, "agent-2", conversation_id="existing-conv-999"
        )

        assert result == expected_ref
        mock_opencode_harness.resume_conversation.assert_called_once_with(
            workspace_root, "existing-conv-999"
        )
        mock_opencode_harness.new_conversation.assert_not_called()

    @pytest.mark.anyio
    async def test_run_turn_completed(self, mock_opencode_harness, workspace_root):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_turn.return_value = turn_ref

        mock_client = AsyncMock()
        mock_opencode_harness._supervisor.get_client.return_value = mock_client

        output_result = OpenCodeTurnOutput(text="Hello from OpenCode", error=None)

        with patch(
            "codex_autorunner.agents.orchestrator.collect_opencode_output",
            new_callable=AsyncMock,
        ) as mock_collect:
            mock_collect.return_value = output_result

            orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
            result = await orchestrator.run_turn(
                workspace_root,
                "conv-2",
                "test prompt",
                model="opencode/gpt-4",
                reasoning="medium",
                approval_mode="auto",
                sandbox_policy=None,
                timeout_seconds=60.0,
            )

            assert result["turn_id"] == "turn-2"
            assert result["conversation_id"] == "conv-2"
            assert result["status"] == TurnStatus.COMPLETED
            assert result["output"] == "Hello from OpenCode"
            assert result["error"] is None

            mock_collect.assert_called_once()
            call_kwargs = mock_collect.call_args.kwargs
            assert call_kwargs["session_id"] == "conv-2"
            assert call_kwargs["workspace_path"] == str(workspace_root)
            assert call_kwargs["permission_policy"] == "auto"
            assert call_kwargs["question_policy"] == "auto_first_option"

    @pytest.mark.anyio
    async def test_run_turn_failed(self, mock_opencode_harness, workspace_root):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_turn.return_value = turn_ref

        mock_client = AsyncMock()
        mock_opencode_harness._supervisor.get_client.return_value = mock_client

        output_result = OpenCodeTurnOutput(text="", error="Authentication failed")

        with patch(
            "codex_autorunner.agents.orchestrator.collect_opencode_output",
            new_callable=AsyncMock,
        ) as mock_collect:
            mock_collect.return_value = output_result

            orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
            result = await orchestrator.run_turn(
                workspace_root, "conv-2", "test prompt"
            )

            assert result["turn_id"] == "turn-2"
            assert result["conversation_id"] == "conv-2"
            assert result["status"] == TurnStatus.FAILED
            assert result["output"] == ""
            assert result["error"] == "Authentication failed"

    @pytest.mark.anyio
    async def test_run_turn_with_approval_mode(
        self, mock_opencode_harness, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_turn.return_value = turn_ref

        mock_client = AsyncMock()
        mock_opencode_harness._supervisor.get_client.return_value = mock_client

        output_result = OpenCodeTurnOutput(text="Output", error=None)

        with patch(
            "codex_autorunner.agents.orchestrator.collect_opencode_output",
            new_callable=AsyncMock,
        ) as mock_collect:
            mock_collect.return_value = output_result

            orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
            await orchestrator.run_turn(
                workspace_root, "conv-2", "test prompt", approval_mode="ask"
            )

            call_kwargs = mock_collect.call_args.kwargs
            assert call_kwargs["permission_policy"] == "ask"

    @pytest.mark.anyio
    async def test_run_turn_without_approval_mode(
        self, mock_opencode_harness, workspace_root
    ):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_turn.return_value = turn_ref

        mock_client = AsyncMock()
        mock_opencode_harness._supervisor.get_client.return_value = mock_client

        output_result = OpenCodeTurnOutput(text="Output", error=None)

        with patch(
            "codex_autorunner.agents.orchestrator.collect_opencode_output",
            new_callable=AsyncMock,
        ) as mock_collect:
            mock_collect.return_value = output_result

            orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
            await orchestrator.run_turn(
                workspace_root, "conv-2", "test prompt", approval_mode=None
            )

            call_kwargs = mock_collect.call_args.kwargs
            assert call_kwargs["permission_policy"] == "allow"

    @pytest.mark.anyio
    async def test_stream_turn_events(self, mock_opencode_harness, workspace_root):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_turn.return_value = turn_ref

        async def stream_events_gen(*args):
            yield '{"event":"data"}'
            yield '{"event":"more"}'

        mock_opencode_harness.stream_events = stream_events_gen

        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        events = []
        async for event in orchestrator.stream_turn_events(
            workspace_root, "conv-2", "test prompt"
        ):
            events.append(event)

        assert len(events) == 4
        assert events[0]["type"] == "turn_started"
        assert events[0]["data"]["turn_id"] == "turn-2"
        assert events[1]["type"] == "event"
        assert events[1]["data"] == '{"event":"data"}'
        assert events[2]["type"] == "event"
        assert events[2]["data"] == '{"event":"more"}'
        assert events[3]["type"] == "turn_completed"
        assert events[3]["data"]["turn_id"] == "turn-2"

    @pytest.mark.anyio
    async def test_interrupt_turn(self, mock_opencode_harness, workspace_root):
        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        result = await orchestrator.interrupt_turn(
            workspace_root, "conv-2", turn_id="turn-2", grace_seconds=15.0
        )

        assert result is True
        mock_opencode_harness.interrupt.assert_called_once_with(
            workspace_root, "conv-2", "turn-2"
        )

    @pytest.mark.anyio
    async def test_interrupt_turn_no_turn_id(
        self, mock_opencode_harness, workspace_root
    ):
        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        result = await orchestrator.interrupt_turn(
            workspace_root, "conv-2", grace_seconds=30.0
        )

        assert result is True
        mock_opencode_harness.interrupt.assert_called_once_with(
            workspace_root, "conv-2", None
        )

    @pytest.mark.anyio
    async def test_start_review(self, mock_opencode_harness, workspace_root):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_review.return_value = turn_ref

        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        result = await orchestrator.start_review(
            workspace_root,
            "conv-2",
            prompt="Review this",
            model="opencode/gpt-4",
            reasoning="high",
            approval_mode="allow",
            sandbox_policy=None,
            timeout_seconds=45.0,
        )

        assert result == turn_ref
        mock_opencode_harness.start_review.assert_called_once_with(
            workspace_root,
            "conv-2",
            "Review this",
            "opencode/gpt-4",
            "high",
            approval_mode="allow",
            sandbox_policy=None,
        )

    @pytest.mark.anyio
    async def test_start_review_no_prompt(self, mock_opencode_harness, workspace_root):
        from codex_autorunner.agents.types import TurnRef

        turn_ref = TurnRef(conversation_id="conv-2", turn_id="turn-2")
        mock_opencode_harness.start_review.return_value = turn_ref

        orchestrator = OpenCodeOrchestrator(mock_opencode_harness)
        result = await orchestrator.start_review(workspace_root, "conv-2")

        assert result == turn_ref
        mock_opencode_harness.start_review.assert_called_once_with(
            workspace_root,
            "conv-2",
            "",
            None,
            None,
            approval_mode=None,
            sandbox_policy=None,
        )
