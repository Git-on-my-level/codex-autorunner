from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_autorunner.agents.opencode.turn_lifecycle import (
    OpenCodeTurnLifecycleState,
    OpenCodeTurnObservation,
    coordinate_turn_lifecycle,
    lifecycle_result_from_observation,
)


@pytest.mark.asyncio
async def test_lifecycle_command_acceptance_waits_for_runtime_observation() -> None:
    release_collector = asyncio.Event()

    async def _command() -> dict[str, Any]:
        return {
            "sessionID": "session-1",
            "parts": [{"type": "text", "text": "transport text"}],
        }

    async def _collect() -> OpenCodeTurnObservation:
        await release_collector.wait()
        return OpenCodeTurnObservation(
            assistant_text="runtime text",
            error=None,
            output_source="event_stream",
            terminal_signal="session.idle",
        )

    command_task = asyncio.create_task(_command())
    collect_task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    result_task = asyncio.create_task(
        coordinate_turn_lifecycle(
            collect_task=collect_task,
            command_task=command_task,
            raw_events=lambda: [],
        )
    )
    await asyncio.sleep(0)

    assert command_task.done()
    assert not result_task.done()

    release_collector.set()
    result = await result_task

    assert result.state == OpenCodeTurnLifecycleState.TERMINAL_OBSERVED
    assert result.assistant_text == "runtime text"
    assert result.output_source == "event_stream"
    assert result.command_completed is True
    assert result.evidence["command_accepted_before_terminal"] is True


@pytest.mark.asyncio
async def test_lifecycle_never_uses_prompt_response_as_output() -> None:
    async def _command() -> dict[str, Any]:
        return {
            "sessionID": "session-1",
            "parts": [{"type": "text", "text": "prompt response text"}],
        }

    async def _collect() -> OpenCodeTurnObservation:
        return OpenCodeTurnObservation(
            assistant_text="",
            error=None,
            output_source="none",
            terminal_signal="session.idle",
        )

    result = await coordinate_turn_lifecycle(
        collect_task=asyncio.create_task(_collect()),
        command_task=asyncio.create_task(_command()),
        raw_events=lambda: [],
    )

    assert result.state == OpenCodeTurnLifecycleState.EMPTY_TERMINAL
    assert result.assistant_text == ""
    assert result.output_source == "none"


@pytest.mark.asyncio
async def test_lifecycle_preserves_prompt_response_error() -> None:
    release_collector = asyncio.Event()

    async def _command() -> dict[str, Any]:
        return {
            "sessionID": "session-1",
            "info": {"error": "prompt rejected"},
        }

    async def _collect() -> OpenCodeTurnObservation:
        await release_collector.wait()
        return OpenCodeTurnObservation(
            assistant_text="",
            error="stream timeout",
            output_source="none",
            terminal_signal=None,
        )

    result = await coordinate_turn_lifecycle(
        collect_task=asyncio.create_task(_collect()),
        command_task=asyncio.create_task(_command()),
        raw_events=lambda: [],
    )

    assert result.state == OpenCodeTurnLifecycleState.FAILED
    assert result.command_completed is True
    assert result.error == "prompt rejected"


def test_lifecycle_snapshot_recovery_is_explicit() -> None:
    result = lifecycle_result_from_observation(
        OpenCodeTurnObservation(
            assistant_text="snapshot text",
            error=None,
            output_source="messages_snapshot",
            terminal_signal=None,
        ),
        raw_events=[],
        command_completed=True,
        command_accepted_before_terminal=True,
        collector_completed=True,
    )

    assert result.state == OpenCodeTurnLifecycleState.SNAPSHOT_RECOVERED
    assert result.snapshot_recovered is True
    assert result.output_source == "messages_snapshot"


@pytest.mark.asyncio
async def test_lifecycle_marks_command_completed_when_failure_follows_collect() -> None:
    async def _command() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("transport failed")

    async def _collect() -> OpenCodeTurnObservation:
        return OpenCodeTurnObservation(
            assistant_text="runtime text",
            error=None,
            output_source="event_stream",
            terminal_signal="session.idle",
        )

    result = await coordinate_turn_lifecycle(
        collect_task=asyncio.create_task(_collect()),
        command_task=asyncio.create_task(_command()),
        raw_events=lambda: [],
    )

    assert result.state == OpenCodeTurnLifecycleState.FAILED
    assert result.command_completed is True
    assert result.error == "transport failed"
