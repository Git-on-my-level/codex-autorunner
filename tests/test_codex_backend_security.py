import json
import logging
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_autorunner.integrations.agents.codex_backend import CodexAppServerBackend


def _make_buffer_logger() -> tuple[logging.Logger, StringIO, logging.Handler]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("test.codex_backend.security")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, stream, handler


@pytest.mark.asyncio
async def test_run_turn_does_not_log_prompt_content():
    """Ensure run_turn does not log prompt content at INFO level."""
    logger, stream, handler = _make_buffer_logger()

    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=Path.cwd(),
        logger=logger,
    )

    with patch.object(
        backend,
        "_ensure_client",
        new_callable=AsyncMock,
    ) as mock_ensure_client:
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.turn_id = "turn-123"
        mock_handle.wait = AsyncMock(
            return_value=MagicMock(
                agent_messages=["Response"],
                raw_events=[],
            )
        )
        mock_client.turn_start = AsyncMock(return_value=mock_handle)
        mock_ensure_client.return_value = mock_client

        backend._thread_id = "thread-456"

        test_message = "My API key is sk-1234567890abcdefghijklmnopqrstuv"
        async for _ in backend.run_turn(session_id="thread-456", message=test_message):
            pass

        handler.flush()
        log_output = stream.getvalue()

        assert "sk-1234567890abcdefghijklmnopqrstuv" not in log_output
        assert "API key is" not in log_output
        assert "message_hash" in log_output
        assert "message_length" in log_output

        payload = json.loads(log_output.strip())
        assert payload["event"] == "agent.turn_started"
        assert "prompt" not in str(payload).lower()
        assert "message" not in payload or payload.get("message") is None


@pytest.mark.asyncio
async def test_run_turn_events_does_not_log_prompt_content():
    """Ensure run_turn_events does not log prompt content at INFO level."""
    logger, stream, handler = _make_buffer_logger()

    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=Path.cwd(),
        logger=logger,
    )

    with patch.object(
        backend,
        "_ensure_client",
        new_callable=AsyncMock,
    ) as mock_ensure_client:
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.turn_id = "turn-789"
        mock_handle.wait = AsyncMock(
            return_value=MagicMock(
                agent_messages=["Response"],
                raw_events=[],
            )
        )
        mock_client.turn_start = AsyncMock(return_value=mock_handle)
        mock_ensure_client.return_value = mock_client

        backend._thread_id = "thread-101"

        test_message = "Secret password: hunter2 and token ghp_1234567890abcdef"
        async for _ in backend.run_turn_events(
            session_id="thread-101", message=test_message
        ):
            pass

        handler.flush()
        log_output = stream.getvalue()

        assert "hunter2" not in log_output
        assert "ghp_1234567890abcdef" not in log_output
        assert "Secret password" not in log_output
        assert "message_hash" in log_output
        assert "message_length" in log_output

        payload = json.loads(log_output.strip())
        assert payload["event"] == "agent.turn_events_started"
        assert "prompt" not in str(payload).lower()


@pytest.mark.asyncio
async def test_multiple_secrets_not_leaked_in_logs():
    """Test that multiple types of secrets are not leaked in logs."""
    logger, stream, handler = _make_buffer_logger()

    backend = CodexAppServerBackend(
        supervisor=MagicMock(),
        workspace_root=Path.cwd(),
        logger=logger,
    )

    with patch.object(
        backend,
        "_ensure_client",
        new_callable=AsyncMock,
    ) as mock_ensure_client:
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.turn_id = "turn-111"
        mock_handle.wait = AsyncMock(
            return_value=MagicMock(
                agent_messages=["Response"],
                raw_events=[],
            )
        )
        mock_client.turn_start = AsyncMock(return_value=mock_handle)
        mock_ensure_client.return_value = mock_client

        backend._thread_id = "thread-222"

        test_message = """
        OpenAI key: sk-proj-1234567890abcdefghijklmnopqrstuv
        GitHub token: ghp_9876543210zyxwvutsrqponmlkjihgfedcba
        AWS key: AKIAIOSFODNN7EXAMPLE
        JWT: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ
        """
        async for _ in backend.run_turn(session_id="thread-222", message=test_message):
            pass

        handler.flush()
        log_output = stream.getvalue()

        assert "sk-proj-1234567890abcdefghijklmnopqrstuv" not in log_output
        assert "ghp_9876543210zyxwvutsrqponmlkjihgfedcba" not in log_output
        assert "AKIAIOSFODNN7EXAMPLE" not in log_output
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in log_output

        payload = json.loads(log_output.strip())
        assert payload["event"] == "agent.turn_started"
        assert payload["message_length"] == len(test_message)
        assert len(payload["message_hash"]) == 16
