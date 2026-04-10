from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.chat_surface_harness.hermes import fake_acp_command

from codex_autorunner.agents.hermes.supervisor import HermesSupervisor


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_waits_for_official_prompt_result_before_completing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    supervisor = HermesSupervisor(fake_acp_command("official_prompt_hang"))
    try:
        caplog.set_level("INFO")
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(tmp_path, session.session_id, "hello")

        with pytest.raises(asyncio.TimeoutError):
            await supervisor.wait_for_turn(
                tmp_path,
                session.session_id,
                turn_id,
                timeout=0.1,
            )

        events = await supervisor.list_turn_events_snapshot(turn_id)
        assert events[0].get("method") == "prompt/started"
        assert all(event.get("method") == "session/update" for event in events[1:])
        assert not any(
            event.get("method")
            in {"prompt/completed", "prompt/failed", "prompt/cancelled"}
            for event in events
        )
        assert "hermes.turn.started" in caplog.text
        assert "hermes.turn.wait_timeout" in caplog.text
        assert f'"session_id":"{session.session_id}"' in caplog.text
        assert f'"turn_id":"{turn_id}"' in caplog.text
        assert '"last_runtime_method":"session/update"' in caplog.text
        assert '"last_progress_at":"' in caplog.text
        assert '"last_session_update_kind":"' in caplog.text
        assert (
            '"last_session_update_kind":"agent_thought_chunk"' in caplog.text
            or '"last_session_update_kind":"agent_message_chunk"' in caplog.text
        )
    finally:
        await supervisor.close_all()
