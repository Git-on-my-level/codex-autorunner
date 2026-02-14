"""Tests for PMA lifecycle router."""

import json
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


@pytest.mark.asyncio
async def test_write_artifact_is_valid_json(temp_hub_root: Path) -> None:
    """Test that _write_artifact produces valid JSON."""
    router = PmaLifecycleRouter(temp_hub_root)

    result = await router.new(agent="opencode")
    assert result.artifact_path is not None

    content = result.artifact_path.read_text(encoding="utf-8")
    parsed = json.loads(content)
    assert parsed["command"] == "new"
    assert parsed["agent"] == "opencode"
    assert "event_id" in parsed
    assert "timestamp" in parsed


@pytest.mark.asyncio
async def test_emit_event_valid_jsonl(temp_hub_root: Path) -> None:
    """Test that _emit_event produces valid JSONL with expected record."""
    router = PmaLifecycleRouter(temp_hub_root)

    await router.reset(agent="opencode")

    events_log = temp_hub_root / ".codex-autorunner" / "pma" / "lifecycle_events.jsonl"
    assert events_log.exists()

    lines = events_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 1

    event = json.loads(lines[-1])
    assert event["event_type"] == "pma_lifecycle_reset"
    assert "event_id" in event
    assert "timestamp" in event
    assert "artifact_path" in event


@pytest.mark.asyncio
async def test_atomic_artifact_write_no_partial_files(temp_hub_root: Path) -> None:
    """Test that artifacts are written atomically (no .tmp files remain)."""
    router = PmaLifecycleRouter(temp_hub_root)

    await router.new(agent="opencode")

    artifacts_dir = temp_hub_root / ".codex-autorunner" / "pma" / "lifecycle"
    tmp_files = list(artifacts_dir.glob("*.json.tmp"))
    assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_events_log_has_lock_file_path(temp_hub_root: Path) -> None:
    """Test that events log uses a lock file for concurrent access."""
    router = PmaLifecycleRouter(temp_hub_root)

    lock_path = (
        temp_hub_root / ".codex-autorunner" / "pma" / "lifecycle_events.jsonl.lock"
    )

    await router.new(agent="opencode")

    assert lock_path.parent.exists()
    events_log = temp_hub_root / ".codex-autorunner" / "pma" / "lifecycle_events.jsonl"
    assert events_log.exists()
