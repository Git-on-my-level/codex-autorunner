"""Tests for PMA lifecycle router."""

from pathlib import Path

import pytest

from codex_autorunner.core.pma_lifecycle import (
    LifecycleCommand,
    PmaLifecycleRouter,
)


@pytest.fixture
def temp_hub_root(tmp_path: Path) -> Path:
    """Create a temporary hub root for testing."""
    return tmp_path / "hub"


@pytest.mark.asyncio
async def test_lifecycle_router_new(temp_hub_root: Path) -> None:
    """Test /new command creates artifact and resets thread."""
    router = PmaLifecycleRouter(temp_hub_root)

    result = await router.new(agent="opencode", lane_id="test:lane")

    assert result.status == "ok"
    assert result.command == LifecycleCommand.NEW
    assert result.artifact_path is not None
    assert "opencode" in result.details.get("agent", "")
    assert "cleared_threads" in result.details
    assert result.artifact_path.exists()

    artifact = result.artifact_path.read_text()
    assert "new" in artifact
    assert "opencode" in artifact


@pytest.mark.asyncio
async def test_lifecycle_router_reset(temp_hub_root: Path) -> None:
    """Test /reset command creates artifact and clears thread state."""
    router = PmaLifecycleRouter(temp_hub_root)

    result = await router.reset(agent="all")

    assert result.status == "ok"
    assert result.command == LifecycleCommand.RESET
    assert result.artifact_path is not None
    assert "all" in result.details.get("agent", "")
    assert "cleared_threads" in result.details
    assert result.artifact_path.exists()


@pytest.mark.asyncio
async def test_lifecycle_router_stop(temp_hub_root: Path) -> None:
    """Test /stop command creates artifact and cancels queue."""
    router = PmaLifecycleRouter(temp_hub_root)

    result = await router.stop(lane_id="test:lane")

    assert result.status == "ok"
    assert result.command == LifecycleCommand.STOP
    assert result.artifact_path is not None
    assert "test:lane" in result.details.get("lane_id", "")
    assert "cancelled_items" in result.details
    assert result.artifact_path.exists()


@pytest.mark.asyncio
async def test_lifecycle_router_compact(temp_hub_root: Path) -> None:
    """Test /compact command creates artifact with summary."""
    router = PmaLifecycleRouter(temp_hub_root)

    summary = "This is a test summary of the compacted conversation."
    result = await router.compact(
        summary=summary, agent="opencode", thread_id="test-thread"
    )

    assert result.status == "ok"
    assert result.command == LifecycleCommand.COMPACT
    assert result.artifact_path is not None
    assert result.details.get("summary_length") == len(summary)
    assert result.artifact_path.exists()

    artifact = result.artifact_path.read_text()
    assert "compact" in artifact
    assert summary in artifact


@pytest.mark.asyncio
async def test_lifecycle_router_idempotent(temp_hub_root: Path) -> None:
    """Test that lifecycle commands are idempotent."""
    router = PmaLifecycleRouter(temp_hub_root)

    result1 = await router.reset(agent="opencode")
    result2 = await router.reset(agent="opencode")

    assert result1.status == "ok"
    assert result2.status == "ok"
    assert result1.artifact_path != result2.artifact_path


@pytest.mark.asyncio
async def test_lifecycle_router_creates_events_log(temp_hub_root: Path) -> None:
    """Test that lifecycle commands emit event records."""
    router = PmaLifecycleRouter(temp_hub_root)

    events_log = temp_hub_root / ".codex-autorunner" / "pma" / "lifecycle_events.jsonl"

    await router.new(agent="opencode")
    await router.reset(agent="opencode")
    await router.stop()
    await router.compact(summary="test summary")

    assert events_log.exists()
    lines = events_log.read_text().strip().split("\n")
    assert len(lines) == 4

    import json

    for line in lines:
        event = json.loads(line)
        assert "event_id" in event
        assert "event_type" in event
        assert "timestamp" in event
        assert "artifact_path" in event


@pytest.mark.asyncio
async def test_lifecycle_router_creates_artifacts_dir(temp_hub_root: Path) -> None:
    """Test that lifecycle router creates artifacts directory."""
    router = PmaLifecycleRouter(temp_hub_root)

    await router.new(agent="opencode")

    artifacts_dir = temp_hub_root / ".codex-autorunner" / "pma" / "lifecycle"
    assert artifacts_dir.exists()
    assert artifacts_dir.is_dir()
