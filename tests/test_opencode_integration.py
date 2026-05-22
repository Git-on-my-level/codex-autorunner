import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import AsyncGenerator, Optional

import pytest

from codex_autorunner.agents.opencode.client import OpenCodeClient
from codex_autorunner.agents.opencode.harness import OpenCodeHarness
from codex_autorunner.agents.opencode.supervisor import (
    OpenCodeSupervisor,
    OpenCodeSupervisorError,
)
from codex_autorunner.core.managed_processes.registry import read_process_record
from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
)
from codex_autorunner.workspace import canonical_workspace_root, workspace_id_for_path


def get_opencode_bin() -> Optional[str]:
    """Get the OpenCode binary path from environment or PATH."""
    opencode_bin = os.environ.get("OPENCODE_BIN")
    if opencode_bin:
        return opencode_bin
    return shutil.which("opencode")


pytestmark = pytest.mark.integration


def _workspace_id(workspace_root: Path) -> str:
    return workspace_id_for_path(canonical_workspace_root(workspace_root))


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _assert_process_gone(pid: int, *, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if not _pid_is_running(pid):
            return
        if loop.time() >= deadline:
            pytest.fail(f"process {pid} still running after cleanup")
        await asyncio.sleep(0.1)


def _assert_registry_removed(workspace_root: Path, pid: int) -> None:
    workspace_id = _workspace_id(workspace_root)
    assert read_process_record(workspace_root, "opencode", workspace_id) is None
    assert read_process_record(workspace_root, "opencode", str(pid)) is None


def _opencode_serve_command() -> list[str]:
    opencode_bin = get_opencode_bin()
    assert opencode_bin is not None
    return [opencode_bin, "serve", "--hostname", "127.0.0.1", "--port", "0"]


def _new_supervisor(
    *,
    request_timeout: float = 30.0,
    max_handles: int = 3,
    idle_ttl_seconds: float = 300.0,
    server_scope: str = "workspace",
) -> OpenCodeSupervisor:
    return OpenCodeSupervisor(
        _opencode_serve_command(),
        request_timeout=request_timeout,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
        server_scope=server_scope,
    )


def _is_environmental_runtime_failure(
    result_errors: list[str], raw_events: list[dict]
) -> bool:
    raw_event_text = json.dumps(raw_events).lower()
    error_text = " ".join(result_errors).lower()
    environmental_markers = (
        "rate limit",
        "api key",
        "auth",
        "unauthorized",
        "quota",
    )
    return any(
        marker in raw_event_text or marker in error_text
        for marker in environmental_markers
    )


def _runtime_failure_detail(
    result_status: str, result_errors: list[str], raw_events: list[dict]
) -> str:
    return (
        f"status={result_status!r} errors={result_errors!r} "
        f"raw_tail={json.dumps(raw_events[-5:], ensure_ascii=False)[:2000]}"
    )


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
    supervisor = _new_supervisor()
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
async def test_supervisor_lifecycle_reuse_and_close(
    supervisor: OpenCodeSupervisor, workspace: Path
) -> None:
    """Supervisor should start OpenCode, reuse workspace handles, and close cleanly."""
    client1 = await supervisor.get_client(workspace)
    client2 = await supervisor.get_client(workspace)
    assert isinstance(client1, OpenCodeClient)
    assert client1 is client2

    await supervisor.close_all()

    client3 = await supervisor.get_client(workspace)
    assert isinstance(client3, OpenCodeClient)
    assert client3 is not client1
    await client3.close()


@pytest.mark.asyncio
async def test_global_scope_uses_single_server_for_two_workspaces(
    tmp_path: Path,
) -> None:
    """Global server scope should reuse one server process across workspaces."""
    workspace1 = tmp_path / "ws1"
    workspace2 = tmp_path / "ws2"
    workspace1.mkdir()
    workspace2.mkdir()
    (workspace1 / ".git").mkdir()
    (workspace2 / ".git").mkdir()

    supervisor = _new_supervisor(server_scope="global")

    try:
        client1 = await supervisor.get_client(workspace1)
        client2 = await supervisor.get_client(workspace2)
        assert client1 is client2
        assert len(supervisor._handles) == 1
        handle = next(iter(supervisor._handles.values()))
        assert handle.process is not None
        assert handle.process.pid is not None
    finally:
        await supervisor.close_all()


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

    client1 = await supervisor.get_client(workspace1)
    handle1 = supervisor._handles[_workspace_id(workspace1)]
    assert handle1.process is not None and handle1.process.pid is not None
    pid1 = handle1.process.pid

    _client2 = await supervisor.get_client(workspace2)
    handle2 = supervisor._handles[_workspace_id(workspace2)]
    assert handle2.process is not None and handle2.process.pid is not None
    pid2 = handle2.process.pid

    _client3 = await supervisor.get_client(workspace3)
    handle3 = supervisor._handles[_workspace_id(workspace3)]
    assert handle3.process is not None and handle3.process.pid is not None
    pid3 = handle3.process.pid

    # This should evict client1 (LRU)
    _client4 = await supervisor.get_client(workspace4)
    handle4 = supervisor._handles[_workspace_id(workspace4)]
    assert handle4.process is not None and handle4.process.pid is not None
    pid4 = handle4.process.pid

    assert _workspace_id(workspace1) not in supervisor._handles
    await _assert_process_gone(pid1)
    _assert_registry_removed(workspace1, pid1)

    await client1.close()
    await supervisor.close_all()
    for workspace_root, pid in (
        (workspace2, pid2),
        (workspace3, pid3),
        (workspace4, pid4),
    ):
        await _assert_process_gone(pid)
        _assert_registry_removed(workspace_root, pid)


@pytest.mark.asyncio
async def test_client_session_catalog_and_stream_contract(workspace: Path) -> None:
    """Client should cover provider, session, and SSE primitives without a model run."""
    supervisor = _new_supervisor(request_timeout=30.0)

    try:
        client = await supervisor.get_client(workspace)

        providers = await client.providers(directory=str(workspace))
        assert isinstance(providers, (dict, list))

        result = await client.create_session(
            title="Test Session",
            directory=str(workspace),
        )
        assert result is not None
        assert "id" in result or "sessionID" in result

        session_id = result.get("id") or result.get("sessionID")
        assert session_id is not None

        sessions = await client.list_sessions(directory=str(workspace))
        assert sessions is not None

        ready_event = asyncio.Event()
        events: list[str] = []

        async def collect_events():
            async for event in client.stream_events(
                directory=str(workspace),
                ready_event=ready_event,
                session_id=session_id,
            ):
                events.append(event.event)
                if events:
                    break

        collect_task = asyncio.create_task(collect_events())
        await asyncio.wait_for(ready_event.wait(), timeout=5.0)
        await asyncio.wait_for(collect_task, timeout=10.0)

        assert events
        await client.close()
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_catalog_conversation_stream_and_interrupt(
    workspace: Path,
) -> None:
    """Harness should expose catalog, conversations, turn streaming, and interrupt."""
    supervisor = _new_supervisor(request_timeout=30.0)
    harness = OpenCodeHarness(supervisor)

    try:
        catalog = await harness.model_catalog(workspace)
        assert catalog.models
        assert catalog.default_model is not None

        conv = await harness.new_conversation(workspace, title="Test Conversation")
        assert conv.agent == "opencode"
        assert conv.id

        conversations = await harness.list_conversations(workspace)
        assert any(item.id == conv.id for item in conversations)

        resumed = await harness.resume_conversation(workspace, conv.id)
        assert resumed.id == conv.id

        turn = await harness.start_turn(
            workspace,
            conv.id,
            prompt="Write a long explanation",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
        assert turn.conversation_id == conv.id
        assert turn.turn_id

        events: list[dict] = []

        async def collect_events():
            async for event in harness.stream_events(workspace, conv.id, turn.turn_id):
                events.append(event)
                if events:
                    break

        collect_task = asyncio.create_task(collect_events())
        await asyncio.wait_for(collect_task, timeout=10.0)
        assert events

        await harness.interrupt(workspace, conv.id, turn.turn_id)
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_harness_wait_for_turn_matches_runtime_event_fallback(
    supervisor: OpenCodeSupervisor,
    workspace: Path,
) -> None:
    """Real OpenCode turns should agree with runtime-event fallback text."""
    if os.environ.get("CAR_RUN_OPENCODE_MODEL_INTEGRATION") != "1":
        pytest.skip(
            "Set CAR_RUN_OPENCODE_MODEL_INTEGRATION=1 to run a live model-backed "
            "OpenCode turn."
        )

    harness = OpenCodeHarness(supervisor)
    conv = await harness.new_conversation(workspace)
    turn = await harness.start_turn(
        workspace,
        conv.id,
        prompt="What is 2+2? Answer with only the number.",
        model=None,
        reasoning=None,
        approval_mode=None,
        sandbox_policy=None,
    )

    try:
        result = await asyncio.wait_for(
            harness.wait_for_turn(workspace, conv.id, turn.turn_id),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        await supervisor.close_all()
        pytest.skip("OpenCode runtime stalled during integration test")

    state = RuntimeThreadRunEventState()
    for raw_event in result.raw_events:
        await normalize_runtime_thread_raw_event(raw_event, state)

    if result.status != "ok":
        await supervisor.close_all()
        if _is_environmental_runtime_failure(result.errors, result.raw_events):
            pytest.skip(
                "OpenCode runtime was blocked by environment/auth/provider state: "
                f"{_runtime_failure_detail(result.status, result.errors, result.raw_events)}"
            )
        pytest.fail(
            "OpenCode runtime returned non-ok status: "
            f"{_runtime_failure_detail(result.status, result.errors, result.raw_events)}"
        )

    assert result.status == "ok"
    assert result.assistant_text.strip()
    assert state.best_assistant_text().strip()
    assert result.assistant_text.strip() == state.best_assistant_text().strip()


@pytest.mark.asyncio
async def test_prune_idle_handles(workspace: Path) -> None:
    """Test that supervisor prunes idle handles."""
    supervisor = _new_supervisor(request_timeout=30.0, idle_ttl_seconds=1.0)

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
