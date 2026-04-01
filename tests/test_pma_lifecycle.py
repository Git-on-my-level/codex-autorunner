"""Tests for PMA lifecycle router."""

import json
from pathlib import Path

import pytest

from codex_autorunner.core.pma_context import default_pma_prompt_state_path
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


@pytest.mark.asyncio
async def test_lifecycle_router_new_clears_scoped_keys(temp_hub_root: Path) -> None:
    """Test that /new clears both global and topic-scoped PMA keys."""
    from codex_autorunner.integrations.app_server.threads import AppServerThreadRegistry

    registry_path = temp_hub_root / ".codex-autorunner" / "app_server_threads.json"
    registry = AppServerThreadRegistry(registry_path)

    registry.set_thread_id("pma", "global-thread-id")
    registry.set_thread_id("pma.-1001.42", "topic-42-thread-id")
    registry.set_thread_id("pma.-1001.99", "topic-99-thread-id")

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.new(agent="codex")

    assert result.status == "ok"
    assert "pma" in result.details.get("cleared_threads", [])
    assert "pma.-1001.42" in result.details.get("cleared_threads", [])
    assert "pma.-1001.99" in result.details.get("cleared_threads", [])

    assert registry.get_thread_id("pma") is None
    assert registry.get_thread_id("pma.-1001.42") is None
    assert registry.get_thread_id("pma.-1001.99") is None


@pytest.mark.asyncio
async def test_lifecycle_router_new_clears_matching_prompt_state_keys(
    temp_hub_root: Path,
) -> None:
    state_path = default_pma_prompt_state_path(temp_hub_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-20T00:00:00Z",
                "sessions": {
                    "pma": {"version": 1},
                    "pma.-1001.42": {"version": 1},
                    "pma.opencode": {"version": 1},
                    "pma.opencode.-1001.42": {"version": 1},
                },
            }
        ),
        encoding="utf-8",
    )

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.new(agent="codex")

    assert result.status == "ok"
    cleared = result.details.get("cleared_prompt_state_keys", [])
    assert "pma" in cleared
    assert "pma.-1001.42" in cleared
    assert "pma.opencode" not in cleared
    assert "pma.opencode.-1001.42" not in cleared

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    sessions = payload.get("sessions", {})
    assert "pma" not in sessions
    assert "pma.-1001.42" not in sessions
    assert "pma.opencode" in sessions
    assert "pma.opencode.-1001.42" in sessions


@pytest.mark.asyncio
async def test_lifecycle_router_reset_clears_scoped_keys(temp_hub_root: Path) -> None:
    """Test that /reset clears both global and topic-scoped PMA keys."""
    from codex_autorunner.integrations.app_server.threads import AppServerThreadRegistry

    registry_path = temp_hub_root / ".codex-autorunner" / "app_server_threads.json"
    registry = AppServerThreadRegistry(registry_path)

    registry.set_thread_id("pma.opencode", "global-opencode-id")
    registry.set_thread_id("pma.opencode.-1001.42", "topic-opencode-42-id")

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.reset(agent="opencode")

    assert result.status == "ok"
    assert "pma.opencode" in result.details.get("cleared_threads", [])
    assert "pma.opencode.-1001.42" in result.details.get("cleared_threads", [])

    assert registry.get_thread_id("pma.opencode") is None
    assert registry.get_thread_id("pma.opencode.-1001.42") is None


@pytest.mark.asyncio
async def test_lifecycle_router_reset_all_clears_all_prompt_state_keys(
    temp_hub_root: Path,
) -> None:
    state_path = default_pma_prompt_state_path(temp_hub_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-20T00:00:00Z",
                "sessions": {
                    "pma": {"version": 1},
                    "pma.-1001.1": {"version": 1},
                    "pma.opencode": {"version": 1},
                    "pma.opencode.-1001.2": {"version": 1},
                },
            }
        ),
        encoding="utf-8",
    )

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.reset(agent="all")

    assert result.status == "ok"
    cleared = result.details.get("cleared_prompt_state_keys", [])
    assert "pma" in cleared
    assert "pma.-1001.1" in cleared
    assert "pma.opencode" in cleared
    assert "pma.opencode.-1001.2" in cleared

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload.get("sessions") == {}


@pytest.mark.asyncio
async def test_lifecycle_router_reset_all_clears_both_agent_scoped_keys(
    temp_hub_root: Path,
) -> None:
    """Test that /reset with agent='all' clears scoped keys for both agents."""
    from codex_autorunner.integrations.app_server.threads import AppServerThreadRegistry

    registry_path = temp_hub_root / ".codex-autorunner" / "app_server_threads.json"
    registry = AppServerThreadRegistry(registry_path)

    registry.set_thread_id("pma", "global-codex-id")
    registry.set_thread_id("pma.-1001.1", "topic-codex-1-id")
    registry.set_thread_id("pma.opencode", "global-opencode-id")
    registry.set_thread_id("pma.opencode.-1001.2", "topic-opencode-2-id")

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.reset(agent="all")

    assert result.status == "ok"
    cleared = result.details.get("cleared_threads", [])
    assert "pma" in cleared
    assert "pma.-1001.1" in cleared
    assert "pma.opencode" in cleared
    assert "pma.opencode.-1001.2" in cleared

    assert registry.get_thread_id("pma") is None
    assert registry.get_thread_id("pma.-1001.1") is None
    assert registry.get_thread_id("pma.opencode") is None
    assert registry.get_thread_id("pma.opencode.-1001.2") is None


@pytest.mark.asyncio
async def test_lifecycle_router_codex_reset_preserves_opencode_scoped_keys(
    temp_hub_root: Path,
) -> None:
    """Codex PMA reset must not clear active opencode topic mappings."""
    from codex_autorunner.integrations.app_server.threads import AppServerThreadRegistry

    registry_path = temp_hub_root / ".codex-autorunner" / "app_server_threads.json"
    registry = AppServerThreadRegistry(registry_path)

    registry.set_thread_id("pma", "global-codex-id")
    registry.set_thread_id("pma.-1001.1", "topic-codex-1-id")
    registry.set_thread_id("pma.opencode", "global-opencode-id")
    registry.set_thread_id("pma.opencode.-1001.2", "topic-opencode-2-id")

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.reset(agent="codex")

    assert result.status == "ok"
    cleared = result.details.get("cleared_threads", [])
    assert "pma" in cleared
    assert "pma.-1001.1" in cleared
    assert "pma.opencode" not in cleared
    assert "pma.opencode.-1001.2" not in cleared

    assert registry.get_thread_id("pma") is None
    assert registry.get_thread_id("pma.-1001.1") is None
    assert registry.get_thread_id("pma.opencode") == "global-opencode-id"
    assert registry.get_thread_id("pma.opencode.-1001.2") == "topic-opencode-2-id"


@pytest.mark.asyncio
async def test_lifecycle_router_codex_reset_preserves_hermes_scoped_state(
    temp_hub_root: Path,
) -> None:
    """Codex PMA reset must preserve non-codex nested agent families."""
    from codex_autorunner.integrations.app_server.threads import AppServerThreadRegistry

    registry_path = temp_hub_root / ".codex-autorunner" / "app_server_threads.json"
    registry = AppServerThreadRegistry(registry_path)
    registry.set_thread_id("pma", "global-codex-id")
    registry.set_thread_id("pma.hermes", "global-hermes-id")
    registry.set_thread_id("pma.hermes.-1001.7", "topic-hermes-7-id")

    state_path = default_pma_prompt_state_path(temp_hub_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-20T00:00:00Z",
                "sessions": {
                    "pma": {"version": 1},
                    "pma.hermes": {"version": 1},
                    "pma.hermes.-1001.7": {"version": 1},
                },
            }
        ),
        encoding="utf-8",
    )

    router = PmaLifecycleRouter(temp_hub_root)
    result = await router.reset(agent="codex")

    assert result.status == "ok"
    cleared_threads = result.details.get("cleared_threads", [])
    assert "pma" in cleared_threads
    assert "pma.hermes" not in cleared_threads
    assert "pma.hermes.-1001.7" not in cleared_threads

    cleared_prompt_state_keys = result.details.get("cleared_prompt_state_keys", [])
    assert "pma" in cleared_prompt_state_keys
    assert "pma.hermes" not in cleared_prompt_state_keys
    assert "pma.hermes.-1001.7" not in cleared_prompt_state_keys

    assert registry.get_thread_id("pma") is None
    assert registry.get_thread_id("pma.hermes") == "global-hermes-id"
    assert registry.get_thread_id("pma.hermes.-1001.7") == "topic-hermes-7-id"

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    sessions = payload.get("sessions", {})
    assert "pma" not in sessions
    assert "pma.hermes" in sessions
    assert "pma.hermes.-1001.7" in sessions
