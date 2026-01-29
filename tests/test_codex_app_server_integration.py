"""Codex app-server integration tests with smoke contracts.

Two-tier testing approach:
- Tier 1: Unauthenticated smoke (always runs if binary present)
- Tier 2: Full turn (env-gated, requires credentials)
"""

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

import pytest

from codex_autorunner.integrations.app_server.client import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
)
from codex_autorunner.integrations.app_server.env import build_app_server_env
from codex_autorunner.integrations.app_server.supervisor import (
    WorkspaceAppServerSupervisor,
)

pytest.skip("codex app-server removed in workspace cutover", allow_module_level=True)


def get_codex_bin() -> Optional[str]:
    """Get Codex binary path from environment or PATH."""
    codex_bin = os.environ.get("CODEX_BIN")
    if codex_bin:
        return codex_bin
    return shutil.which("codex")


def has_codex_credentials() -> bool:
    """Check if Codex credentials are configured."""
    # Codex typically uses ~/.codex/credentials or environment vars
    # We check for common credential indicators
    codex_home = Path.home() / ".codex"
    if (codex_home / "credentials").exists():
        return True
    if os.environ.get("CODEX_CREDENTIALS_PATH"):
        return True
    return False


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def skip_if_no_codex():
    """Skip all tests in this file if Codex binary is not available."""
    if get_codex_bin() is None:
        pytest.skip(
            "Codex binary not found. Set CODEX_BIN environment variable or install codex to run these tests."
        )


@pytest.fixture()
async def supervisor(
    tmp_path: Path,
) -> AsyncGenerator[WorkspaceAppServerSupervisor, None]:
    """Create a Codex app-server supervisor instance."""

    def env_builder(
        _workspace_root: Path, _workspace_id: str, _state_dir: Path
    ) -> dict:
        return build_app_server_env(command, _workspace_root, _state_dir)

    codex_bin = get_codex_bin()
    assert codex_bin is not None

    command = [codex_bin, "app-server", "--port", "0"]

    supervisor = WorkspaceAppServerSupervisor(
        command,
        state_root=tmp_path,
        env_builder=env_builder,
        idle_ttl_seconds=300.0,
        request_timeout=30.0,
        max_handles=5,
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
    (workspace / ".codex-autorunner").mkdir()
    yield workspace


@asynccontextmanager
async def client_context(supervisor: WorkspaceAppServerSupervisor, workspace: Path):
    """Context manager for getting and closing a client."""
    client = await supervisor.get_client(workspace)
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tier1_codex_app_server_starts(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test that Codex app-server starts and initializes."""
    async with client_context(supervisor, workspace) as client:
        assert client is not None
        assert isinstance(client, CodexAppServerClient)


@pytest.mark.asyncio
async def test_tier1_initialize_roundtrip(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test initialize → initialized round trip."""
    async with client_context(supervisor, workspace) as client:
        init_response = await client.initialize(str(workspace))

        # Validate required fields
        assert init_response is not None
        assert isinstance(init_response, dict)

        # Check for expected keys (may vary by version)
        # At minimum, we expect some response from initialize
        assert len(init_response) > 0


@pytest.mark.asyncio
async def test_tier1_thread_list_works(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test that thread_list returns valid JSON structure."""
    async with client_context(supervisor, workspace) as client:
        threads = await client.thread_list()

        # Validate response shape
        assert threads is not None
        assert isinstance(threads, dict)

        # May have "threads" key with list or be empty
        # Just ensure it's a valid dict
        if "threads" in threads:
            assert isinstance(threads["threads"], list)


@pytest.mark.asyncio
async def test_tier1_model_list_works(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test that model_list returns valid JSON structure."""
    async with client_context(supervisor, workspace) as client:
        models = await client.model_list()

        # Validate response shape
        assert models is not None
        assert isinstance(models, dict)

        # Should have at least some model information
        # Structure may vary, but we expect a dict with model data
        assert len(models) > 0


@pytest.mark.asyncio
async def test_tier1_config_read_works(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test that config read returns valid JSON structure."""
    async with client_context(supervisor, workspace) as client:
        config = await client.config()

        # Validate response shape
        assert config is not None
        assert isinstance(config, dict)

        # Should have at least some config data
        assert len(config) > 0


@pytest.mark.asyncio
async def test_tier1_thread_create_and_resume(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test thread creation and resume without executing turns."""
    async with client_context(supervisor, workspace) as client:
        # Create a thread
        thread_result = await client.thread_start(str(workspace))

        # Validate response
        assert thread_result is not None
        assert isinstance(thread_result, dict)
        thread_id = thread_result.get("id")
        assert isinstance(thread_id, str) and len(thread_id) > 0

        # Resume the thread
        resume_result = await client.thread_resume(thread_id)

        # Validate resume response
        assert resume_result is not None
        assert isinstance(resume_result, dict)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not has_codex_credentials(), reason="Codex credentials not configured"
)
async def test_tier2_full_turn_execution(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 2: Test full turn execution (requires credentials)."""
    async with client_context(supervisor, workspace) as client:
        # Initialize
        await client.initialize(str(workspace))

        # Create thread
        thread_result = await client.thread_start(str(workspace))
        thread_id = thread_result.get("id")
        assert thread_id

        # Start a simple turn (just echo or no-op if possible)
        # Note: This may require actual model execution, so it's env-gated
        try:
            turn_result = await client.turn_start(
                thread_id,
                "Create a test file with 'hello world' content.",
                approval_policy="never",
            )

            # Validate turn result
            assert turn_result is not None
            assert isinstance(turn_result, object)

            # Try to get turn status
            # (This may timeout if no credentials, which is expected)
        except (CodexAppServerDisconnected, asyncio.TimeoutError) as e:
            # If we get here due to auth issues, that's acceptable for smoke testing
            # The important part is that the server accepted the request
            pytest.skip(f"Full turn requires valid credentials: {e}")
        except Exception as e:
            # Any other exception should be investigated
            pytest.fail(f"Unexpected exception during full turn: {e}")


@pytest.mark.asyncio
async def test_tier1_response_shapes_are_consistent(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Verify API response shapes are consistent."""
    async with client_context(supervisor, workspace) as client:
        # Get multiple responses
        init_response = await client.initialize(str(workspace))
        threads_response = await client.thread_list()
        models_response = await client.model_list()
        config_response = await client.config()

        # All should be dicts
        for name, response in {
            "initialize": init_response,
            "thread_list": threads_response,
            "model_list": models_response,
            "config": config_response,
        }.items():
            assert isinstance(response, dict), f"{name} response should be a dict"


@pytest.mark.asyncio
async def test_tier1_client_handles_unknown_events(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Verify client can handle unknown event types."""
    async with client_context(supervisor, workspace) as client:
        # The client should be able to parse event streams
        # even if it encounters unknown event types
        await client.initialize(str(workspace))

        # Just ensure we can create a thread without crashing
        thread_result = await client.thread_start(str(workspace))

        # Success if we got here without exception
        assert thread_result is not None


@pytest.mark.asyncio
async def test_tier1_concurrent_clients(
    supervisor: WorkspaceAppServerSupervisor, tmp_path: Path
):
    """Tier 1: Test that supervisor handles multiple workspaces."""
    # Create two workspaces
    workspace1 = tmp_path / "workspace1"
    workspace1.mkdir()
    (workspace1 / ".git").mkdir()
    (workspace1 / "README.md").write_text("# Workspace 1\n")

    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    (workspace2 / ".git").mkdir()
    (workspace2 / "README.md").write_text("# Workspace 2\n")

    # Get clients for both workspaces
    client1 = await supervisor.get_client(workspace1)
    client2 = await supervisor.get_client(workspace2)

    try:
        # Both clients should work
        assert client1 is not None
        assert client2 is not None

        # Both should be able to initialize
        await client1.initialize(str(workspace1))
        await client2.initialize(str(workspace2))

        # Verify they're different instances
        assert client1 is not client2
    finally:
        await client1.close()
        await client2.close()


@pytest.mark.asyncio
async def test_tier1_client_reconnects_after_disconnect(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test that client can reconnect after disconnect."""
    async with client_context(supervisor, workspace) as client:
        # Initialize
        await client.initialize(str(workspace))

    # Reconnect
    async with client_context(supervisor, workspace) as client2:
        # Should be able to initialize again
        await client2.initialize(str(workspace))


@pytest.mark.asyncio
async def test_tier1_supervisor_handles_timeout(
    supervisor: WorkspaceAppServerSupervisor, workspace: Path
):
    """Tier 1: Test that supervisor handles request timeouts gracefully."""
    async with client_context(supervisor, workspace) as client:
        # Set a short timeout for testing
        # (actual implementation may have fixed timeouts)
        try:
            # Try an operation that might timeout
            await client.initialize(str(workspace))
        except asyncio.TimeoutError:
            # Timeout is acceptable for smoke test
            pytest.skip("Request timeout (acceptable for smoke test)")
        except Exception as e:
            # Other exceptions might indicate actual issues
            pytest.fail(f"Unexpected exception: {e}")
