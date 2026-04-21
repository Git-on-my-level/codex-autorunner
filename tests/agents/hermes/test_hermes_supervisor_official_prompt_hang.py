from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.chat_surface_harness.hermes import fake_acp_command

from codex_autorunner.agents.hermes.supervisor import (
    HermesSupervisor,
    _probe_hermes_session_store_root,
)


@pytest.mark.parametrize(
    ("stdout", "relative_path"),
    [
        (
            "hermes_home ~/.hermes/profiles/hermes-m4-pma\n",
            Path(".hermes/profiles/hermes-m4-pma"),
        ),
        (
            "--- hermes dump ---\n"
            "profile: hermes-m4-pma\n"
            "hermes_home:      ~/.hermes/profiles/hermes-m4-pma\n"
            "--- end dump ---\n",
            Path(".hermes/profiles/hermes-m4-pma"),
        ),
    ],
)
def test_probe_hermes_session_store_root_parses_dump_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    relative_path: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def _fake_run(*args, **kwargs):
        _ = args, kwargs
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(
        "codex_autorunner.agents.hermes.supervisor.subprocess.run",
        _fake_run,
    )

    expected = (tmp_path / "home" / relative_path).resolve()
    assert _probe_hermes_session_store_root("/tmp/hermes", {}) == expected


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
async def test_hermes_supervisor_recovers_second_prompt_from_persisted_session_store(
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
        second_result = await asyncio.wait_for(
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
        assert second_result.status == "completed"
        assert second_result.assistant_text == same_text
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "session/update",
            "session/update",
            "prompt/completed",
        ]
        assert events[-1].get("params", {}).get("recoveredFrom") == "session_store"
        assert "hermes.turn.recovered_from_session_store" in caplog.text
        assert "hermes.turn.wait_timeout_recovered" in caplog.text
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_wait_for_turn_recovers_without_active_turn_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = HermesSupervisor(fake_acp_command("official_prompt_hang"))
    try:

        async def _fake_snapshot(_session_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                message_count=4,
                last_updated_unix=time.time(),
                last_assistant_text="persisted reply",
            )

        monkeypatch.setattr(
            supervisor,
            "_read_session_store_snapshot",
            _fake_snapshot,
        )

        result = await supervisor.wait_for_turn(
            tmp_path,
            "session-1",
            "turn-9",
        )

        assert result.status == "completed"
        assert result.assistant_text == "persisted reply"
        assert [event.get("method") for event in result.raw_events] == [
            "prompt/completed"
        ]
        assert (
            result.raw_events[0].get("params", {}).get("recoveredFrom")
            == "session_store_missing_state"
        )
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_recovers_session_store_without_active_turn_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = HermesSupervisor(fake_acp_command("official_prompt_hang"))
    try:

        async def _fake_snapshot(_session_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                message_count=4,
                last_updated_unix=time.time(),
                last_assistant_text="persisted reply",
            )

        monkeypatch.setattr(
            supervisor,
            "_read_session_store_snapshot",
            _fake_snapshot,
        )

        result = await supervisor.recover_turn_from_session_store(
            tmp_path,
            "session-1",
            "turn-9",
        )

        assert result is not None
        assert result.status == "completed"
        assert result.assistant_text == "persisted reply"
        assert [event.get("method") for event in result.raw_events] == [
            "prompt/completed"
        ]
        assert (
            result.raw_events[0].get("params", {}).get("recoveredFrom")
            == "session_store_missing_state"
        )
    finally:
        await supervisor.close_all()
