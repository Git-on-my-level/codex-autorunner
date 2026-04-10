from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.chat_surface_harness.hermes import (
    SESSION_STATUS_IDLE_COMPLETION_GAP,
    fake_acp_command,
)

from codex_autorunner.agents.hermes.supervisor import HermesSupervisor


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_completes_turn_when_runtime_goes_idle_without_prompt_completed(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fake_acp_command(SESSION_STATUS_IDLE_COMPLETION_GAP))
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(tmp_path, session.session_id, "hello")

        result = await asyncio.wait_for(
            supervisor.wait_for_turn(tmp_path, session.session_id, turn_id),
            timeout=1.0,
        )

        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert any(
            event.get("method") == "session.status"
            and event.get("params", {}).get("status", {}).get("type") == "idle"
            for event in result.raw_events
        )
    finally:
        await supervisor.close_all()
