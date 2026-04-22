from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.chat_surface_harness.hermes import fake_acp_command

from codex_autorunner.agents.hermes.supervisor import (
    HermesSupervisorError,
    HermesSupervisor,
)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_times_out_when_official_prompt_never_emits_terminal_state(
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_completes_from_terminal_event_without_request_return(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(
        fake_acp_command("official_terminal_without_request_return")
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(tmp_path, session.session_id, "hello")

        result = await asyncio.wait_for(
            supervisor.wait_for_turn(
                tmp_path,
                session.session_id,
                turn_id,
            ),
            timeout=2.0,
        )

        events = await supervisor.list_turn_events_snapshot(turn_id)
        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "session/update",
            "session/update",
            "prompt/completed",
        ]
    finally:
        await supervisor.close_all()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_does_not_treat_persisted_session_store_as_terminal_completion(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    supervisor = HermesSupervisor(
        fake_acp_command("official_second_prompt_hang_with_persisted_completion"),
        base_env={"HERMES_HOME": str(hermes_home)},
    )
    try:
        caplog.set_level("INFO")
        session = await supervisor.create_session(tmp_path)
        first_turn_id = await supervisor.start_turn(
            tmp_path, session.session_id, "first"
        )
        first_result = await asyncio.wait_for(
            supervisor.wait_for_turn(tmp_path, session.session_id, first_turn_id),
            timeout=2.0,
        )

        # Second prompt is different, but the assistant output is the same as the
        # first turn (see fake_acp_server official_second_prompt_hang...).
        second_turn_id = await supervisor.start_turn(
            tmp_path, session.session_id, "second"
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                supervisor.wait_for_turn(
                    tmp_path,
                    session.session_id,
                    second_turn_id,
                    timeout=0.2,
                ),
                timeout=2.0,
            )

        events = await supervisor.list_turn_events_snapshot(second_turn_id)
        same_text = "identical fixture output"
        assert first_result.assistant_text == same_text
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "session/update",
            "session/update",
        ]
        assert "hermes.turn.recovered_from_session_store" not in caplog.text
        assert "hermes.turn.wait_timeout_recovered" not in caplog.text
        assert "hermes.turn.wait_timeout" in caplog.text
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_wait_for_turn_requires_active_turn_state(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fake_acp_command("official_prompt_hang"))
    try:
        with pytest.raises(HermesSupervisorError, match="Unknown Hermes turn"):
            await supervisor.wait_for_turn(
                tmp_path,
                "session-1",
                "turn-9",
            )
    finally:
        await supervisor.close_all()
