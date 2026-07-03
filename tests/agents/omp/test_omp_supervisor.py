from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codex_autorunner.agents.acp import ACPMissingSessionError
from codex_autorunner.agents.base import UnsupportedAgentCapabilityError
from codex_autorunner.agents.omp import OMPHarness, OMPSupervisor
from codex_autorunner.core.orchestration.interfaces import (
    FreshConversationRequiredError,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"
pytestmark = pytest.mark.slow


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


@pytest.mark.asyncio
async def test_omp_create_session_carries_config_options(tmp_path: Path) -> None:
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    try:
        session = await supervisor.create_session(tmp_path, title="omp-fixture")
        assert session.session_id
        assert "configOptions" in session.raw
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_model_catalog_from_config_options_before_any_conversation(
    tmp_path: Path,
) -> None:
    """model_listing must work when called before any conversation is created."""
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    harness = OMPHarness(supervisor)
    try:
        await harness.ensure_ready(tmp_path)
        catalog = await harness.model_catalog(tmp_path)
        assert catalog.default_model == "zai/glm-5.2"
        assert [m.id for m in catalog.models] == ["zai/glm-5.2", "zai/glm-4.5"]
        assert catalog.models[0].supports_reasoning is True
        assert catalog.models[0].reasoning_options == ["high"]
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_missing_session_load_maps_via_matcher(tmp_path: Path) -> None:
    """OMP's -32603 'session not found' must surface as ACPMissingSessionError (G1)."""
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    try:
        with pytest.raises(ACPMissingSessionError):
            await supervisor.resume_session(tmp_path, "does-not-exist")
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_sparse_session_load_accepted(tmp_path: Path) -> None:
    """OMP returns a sparse {} on load (no sessionId); resolve via caller-known id."""
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    try:
        created = await supervisor.create_session(tmp_path)
        resumed = await supervisor.resume_session(tmp_path, created.session_id)
        assert resumed.session_id == created.session_id
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_harness_resume_missing_raises_fresh_conversation_required(
    tmp_path: Path,
) -> None:
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    harness = OMPHarness(supervisor)
    try:
        await harness.ensure_ready(tmp_path)
        with pytest.raises(FreshConversationRequiredError):
            await harness.resume_conversation(tmp_path, "does-not-exist")
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_active_thread_discovery(tmp_path: Path) -> None:
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    try:
        await supervisor.create_session(tmp_path, title="thread-a")
        sessions = await supervisor.list_sessions(tmp_path)
        assert any(s.session_id for s in sessions)
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_harness_interrupt_raises_capability_error(tmp_path: Path) -> None:
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    harness = OMPHarness(supervisor)
    try:
        await harness.ensure_ready(tmp_path)
        with pytest.raises(UnsupportedAgentCapabilityError) as exc_info:
            await harness.interrupt(tmp_path, "session-1", "turn-1")
        assert exc_info.value.capability == "interrupt"
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_omp_wait_for_turn_ignores_requested_model_override(
    tmp_path: Path,
) -> None:
    supervisor = OMPSupervisor(fixture_command("omp"), request_timeout=10.0)
    harness = OMPHarness(supervisor)
    try:
        await harness.ensure_ready(tmp_path)
        conversation = await harness.new_conversation(tmp_path)
        turn = await harness.start_turn(
            tmp_path,
            conversation.id,
            "ping",
            model="other/provider-model",
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
        result = await harness.wait_for_turn(
            tmp_path,
            conversation.id,
            turn.turn_id,
            timeout=10.0,
        )
        assert result.effective_runtime is not None
        assert result.effective_runtime["source"] == "omp_session_models"
        assert result.effective_runtime["provider_payload"]["modelID"] == "zai/glm-5.2"
        assert result.effective_runtime["provider_payload"]["modelID"] != (
            "other/provider-model"
        )
    finally:
        await supervisor.close_all()
