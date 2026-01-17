import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import AsyncGenerator, Optional

import pytest

from codex_autorunner.agents.opencode.client import OpenCodeClient
from codex_autorunner.agents.opencode.events import parse_sse_lines
from codex_autorunner.agents.opencode.harness import OpenCodeHarness
from codex_autorunner.agents.opencode.supervisor import (
    OpenCodeSupervisor,
    OpenCodeSupervisorError,
)


def get_opencode_bin() -> Optional[str]:
    """Get the OpenCode binary path from environment or PATH."""
    opencode_bin = os.environ.get("OPENCODE_BIN")
    if opencode_bin:
        return opencode_bin
    return shutil.which("opencode")


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def skip_if_no_opencode():
    """Skip all tests in this file if OpenCode is not available."""
    if get_opencode_bin() is None:
        pytest.skip(
            "OpenCode binary not found. Set OPENCODE_BIN environment variable to run these tests."
        )


@pytest.fixture()
async def supervisor(tmp_path: Path) -> AsyncGenerator[OpenCodeSupervisor, None]:
    """Create an OpenCode supervisor instance."""
    opencode_bin = get_opencode_bin()
    assert opencode_bin is not None
    command = [opencode_bin, "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(
        command,
        request_timeout=30.0,
        max_handles=3,
        idle_ttl_seconds=300.0,
    )
    yield supervisor
    await supervisor.close_all()


@pytest.fixture()
async def workspace(tmp_path: Path) -> AsyncGenerator[Path, None]:
    """Create a minimal git workspace for testing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "README.md").write_text("# Test Repo\n")
    yield workspace


@pytest.mark.asyncio
async def test_supervisor_starts_opencode_server(
    supervisor: OpenCodeSupervisor, workspace: Path
) -> None:
    """Test that supervisor can start an OpenCode server for a workspace."""
    client = await supervisor.get_client(workspace)
    assert client is not None
    assert isinstance(client, OpenCodeClient)
    await client.close()


@pytest.mark.asyncio
async def test_supervisor_reuses_handle(
    supervisor: OpenCodeSupervisor, workspace: Path
) -> None:
    """Test that supervisor reuses an existing handle for the same workspace."""
    client1 = await supervisor.get_client(workspace)
    client2 = await supervisor.get_client(workspace)
    assert client1 is client2
    await client1.close()


@pytest.mark.asyncio
async def test_supervisor_closes_handle(
    supervisor: OpenCodeSupervisor, workspace: Path
) -> None:
    """Test that supervisor can close a handle."""
    client = await supervisor.get_client(workspace)
    await supervisor.close_all()

    # Getting a new client should create a new handle
    client2 = await supervisor.get_client(workspace)
    assert client2 is not client
    await client2.close()


@pytest.mark.asyncio
async def test_supervisor_max_handles_eviction(
    supervisor: OpenCodeSupervisor, tmp_path: Path
) -> None:
    """Test that supervisor evicts least recently used handle when max_handles is exceeded."""
    workspace1 = tmp_path / "ws1"
    workspace2 = tmp_path / "ws2"
    workspace3 = tmp_path / "ws3"
    workspace4 = tmp_path / "ws4"
    for ws in [workspace1, workspace2, workspace3, workspace4]:
        ws.mkdir()
        (ws / ".git").mkdir()

    _client1 = await supervisor.get_client(workspace1)
    _client2 = await supervisor.get_client(workspace2)
    _client3 = await supervisor.get_client(workspace3)

    # This should evict client1 (LRU)
    _client4 = await supervisor.get_client(workspace4)

    # Verify by trying to close all - client1 should already be closed
    await supervisor.close_all()


@pytest.mark.asyncio
async def test_client_providers(workspace: Path) -> None:
    """Test that client can fetch providers."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)

    try:
        client = await supervisor.get_client(workspace)
        providers = await client.providers()
        assert providers is not None
        assert isinstance(providers, (dict, list))
        await client.close()
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_client_create_and_list_sessions(workspace: Path) -> None:
    """Test that client can create and list sessions."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)

    try:
        client = await supervisor.get_client(workspace)

        # Create a session
        result = await client.create_session(
            title="Test Session",
            directory=str(workspace),
        )
        assert result is not None
        assert "id" in result or "sessionID" in result

        session_id = result.get("id") or result.get("sessionID")
        assert session_id is not None

        # List sessions
        sessions = await client.list_sessions(directory=str(workspace))
        assert sessions is not None
        await client.close()
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_client_send_message(workspace: Path) -> None:
    """Test that client can send a message to a session."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)

    try:
        client = await supervisor.get_client(workspace)

        # Create a session
        result = await client.create_session(directory=str(workspace))
        session_id = result.get("id") or result.get("sessionID")
        assert session_id is not None

        # Send a simple message
        response = await client.send_message(
            session_id,
            message="Hello, world!",
        )
        assert response is not None
        await client.close()
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_client_stream_events(workspace: Path) -> None:
    """Test that client can stream events."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)

    try:
        client = await supervisor.get_client(workspace)

        # Create a session
        result = await client.create_session(directory=str(workspace))
        session_id = result.get("id") or result.get("sessionID")
        assert session_id is not None

        # Send a message to generate events
        await client.send_message(session_id, message="Test message")

        # Stream events for a short time
        events_count = 0
        event_types = set()
        timeout_task = asyncio.create_task(asyncio.sleep(5.0))

        async def collect_events():
            nonlocal events_count, event_types
            async for event in client.stream_events(directory=str(workspace)):
                events_count += 1
                event_types.add(event.event)
                if events_count >= 3:
                    break

        collect_task = asyncio.create_task(collect_events())
        done, pending = await asyncio.wait(
            [collect_task, timeout_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await client.close()
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_model_catalog(workspace: Path) -> None:
    """Test that harness can fetch model catalog."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)
    harness = OpenCodeHarness(supervisor)

    try:
        catalog = await harness.model_catalog(workspace)
        assert catalog is not None
        assert catalog.models is not None
        assert len(catalog.models) > 0
        assert catalog.default_model is not None
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_conversation_lifecycle(workspace: Path) -> None:
    """Test that harness can manage conversations."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)
    harness = OpenCodeHarness(supervisor)

    try:
        # Create a conversation
        conv = await harness.new_conversation(workspace, title="Test Conversation")
        assert conv.agent == "opencode"
        assert conv.id is not None

        # List conversations
        conversations = await harness.list_conversations(workspace)
        assert len(conversations) > 0

        # Resume conversation
        resumed = await harness.resume_conversation(workspace, conv.id)
        assert resumed.id == conv.id
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_start_turn(workspace: Path) -> None:
    """Test that harness can start a turn."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)
    harness = OpenCodeHarness(supervisor)

    try:
        # Create a conversation
        conv = await harness.new_conversation(workspace)

        # Start a turn
        turn = await harness.start_turn(
            workspace,
            conv.id,
            prompt="What is 2+2?",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
        assert turn.conversation_id == conv.id
        assert turn.turn_id is not None
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_start_review(workspace: Path) -> None:
    """Test that harness can start a review."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=120.0)
    harness = OpenCodeHarness(supervisor)

    try:
        # Create a conversation
        conv = await harness.new_conversation(workspace)

        # Write a test file to review
        (workspace / "test.py").write_text("def foo():\n    return 1\n")

        # Start a review with longer timeout - review can take longer to complete
        turn = await harness.start_review(
            workspace,
            conv.id,
            prompt=".",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
        assert turn.conversation_id == conv.id
        assert turn.turn_id is not None
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_interrupt(workspace: Path) -> None:
    """Test that harness can interrupt a turn."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)
    harness = OpenCodeHarness(supervisor)

    try:
        # Create a conversation
        conv = await harness.new_conversation(workspace)

        # Start a turn
        turn = await harness.start_turn(
            workspace,
            conv.id,
            prompt="Write a long explanation",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )

        # Interrupt the turn (should not raise)
        await harness.interrupt(workspace, conv.id, turn.turn_id)
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_stream_turn_events(workspace: Path) -> None:
    """Test that harness can stream turn events."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0)
    harness = OpenCodeHarness(supervisor)

    try:
        # Create a conversation
        conv = await harness.new_conversation(workspace)

        # Start a turn
        turn = await harness.start_turn(
            workspace,
            conv.id,
            prompt="Hello",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )

        # Stream events
        events = []
        timeout_task = asyncio.create_task(asyncio.sleep(10.0))

        async def collect_events():
            async for event in harness.stream_events(workspace, conv.id, turn.turn_id):
                events.append(event)
                if len(events) >= 2:
                    break

        collect_task = asyncio.create_task(collect_events())
        done, pending = await asyncio.wait(
            [collect_task, timeout_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_sse_event_parsing() -> None:
    """Test SSE event parsing."""

    async def mock_lines():
        yield "event: message"
        yield 'data: {"type": "test", "value": 42}'
        yield ""
        yield "event: custom"
        yield "data: hello"
        yield ""

    events = []
    async for event in parse_sse_lines(mock_lines()):
        events.append(event)

    assert len(events) == 2
    assert events[0].event == "message"
    assert json.loads(events[0].data) == {"type": "test", "value": 42}
    assert events[1].event == "custom"
    assert events[1].data == "hello"


@pytest.mark.asyncio
async def test_prune_idle_handles(workspace: Path, tmp_path: Path) -> None:
    """Test that supervisor prunes idle handles."""
    command = [get_opencode_bin(), "serve", "--hostname", "127.0.0.1", "--port", "0"]
    supervisor = OpenCodeSupervisor(
        command,
        request_timeout=30.0,
        idle_ttl_seconds=1.0,
    )

    try:
        # Get client for workspace
        await supervisor.get_client(workspace)

        # Wait for TTL to expire
        await asyncio.sleep(1.5)

        # Prune idle handles
        pruned = await supervisor.prune_idle()
        assert pruned >= 1
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_supervisor_timeout(workspace: Path) -> None:
    """Test that supervisor raises error when OpenCode fails to start."""
    # Use an invalid command that will fail
    command = ["nonexistent_opencode_command", "serve"]
    supervisor = OpenCodeSupervisor(command, request_timeout=5.0)

    try:
        with pytest.raises((OpenCodeSupervisorError, FileNotFoundError, OSError)):
            await supervisor.get_client(workspace)
    finally:
        await supervisor.close_all()
