from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from codex_autorunner.agents.hermes.supervisor import HermesSupervisor

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


async def _collect_events(
    supervisor: HermesSupervisor,
    workspace_root: Path,
    session_id: str,
    turn_id: str,
) -> list[dict[str, object]]:
    return [
        event
        async for event in supervisor.stream_turn_events(
            workspace_root,
            session_id,
            turn_id,
        )
    ]


@pytest.mark.asyncio
async def test_hermes_supervisor_session_roundtrip_and_turn_streaming(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("basic"))
    try:
        await supervisor.ensure_ready(tmp_path)
        created = await supervisor.create_session(tmp_path, title="Fixture Session")
        resumed = await supervisor.resume_session(tmp_path, created.session_id)
        listed = await supervisor.list_sessions(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            created.session_id,
            "hello from hermes",
            model="openrouter/gpt-5-mini",
        )
        stream_task = asyncio.create_task(
            _collect_events(supervisor, tmp_path, created.session_id, turn_id)
        )
        result = await supervisor.wait_for_turn(
            tmp_path,
            created.session_id,
            turn_id,
        )
        events = await stream_task

        assert created.session_id == resumed.session_id
        assert [session.session_id for session in listed] == [created.session_id]
        assert turn_id == "turn-1"
        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "prompt/progress",
            "prompt/progress",
            "prompt/completed",
        ]
        assert [event.get("method") for event in result.raw_events] == [
            "prompt/started",
            "prompt/progress",
            "prompt/progress",
            "prompt/completed",
        ]
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_can_interrupt_active_turn_without_explicit_turn_id(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("basic"))
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(tmp_path, session.session_id, "cancel me")
        await supervisor.interrupt_turn(tmp_path, session.session_id, None)
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "cancelled"
        assert result.assistant_text == "fixture "
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_rejects_unknown_turn_lookup(tmp_path: Path) -> None:
    supervisor = HermesSupervisor(fixture_command("basic"))
    try:
        session = await supervisor.create_session(tmp_path)

        with pytest.raises(Exception, match="No active Hermes turn tracked"):
            await supervisor.interrupt_turn(tmp_path, session.session_id, None)
    finally:
        await supervisor.close_all()
