from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.agents.acp import (
    ACPClient,
    ACPInitializationError,
    ACPMissingSessionError,
    ACPPermissionRequestEvent,
)
from codex_autorunner.agents.acp.errors import (
    ACPProcessCrashedError,
    ACPProtocolError,
)
from codex_autorunner.agents.acp.protocol import (
    ACPSessionForkResult,
    ACPSetModelResult,
    ACPSetModeResult,
    extract_advertised_commands,
    extract_session_capabilities,
)
from codex_autorunner.core.acp_lifecycle import (
    _IDLE_TERMINAL_METHODS,
    _SESSION_TURN_ID_FALLBACK_METHODS,
    _TERMINAL_METHODS,
)
from codex_autorunner.core.acp_lifecycle import (
    should_map_missing_turn_id as _shared_should_map_missing_turn_id,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"
pytestmark = pytest.mark.slow


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


@pytest.mark.parametrize(("method"), ("session/update", "session/request_permission"))
def test_client_maps_session_scoped_official_events_without_turn_id(
    method: str,
) -> None:
    client = ACPClient(fixture_command("official"))
    client._session_active_turns["session-1"] = "turn-2"

    message = client._message_with_mapped_turn_id(
        {
            "method": method,
            "params": {
                "sessionId": "session-1",
                "update": {"sessionUpdate": "agent_message_chunk"},
            },
        }
    )

    assert message["params"]["turnId"] == "turn-2"


@pytest.mark.parametrize(
    ("case"),
    load_acp_lifecycle_corpus(),
    ids=[case["name"] for case in load_acp_lifecycle_corpus()],
)
def test_client_maps_shared_lifecycle_fixtures_without_turn_id(
    case: dict[str, object],
) -> None:
    client = ACPClient(fixture_command("official"))
    client._session_active_turns["session-1"] = "turn-2"
    message = dict(case["raw"])  # type: ignore[index]
    expected = dict(case["expected"])  # type: ignore[index]

    mapped = client._message_with_mapped_turn_id(message)

    expected_turn_id = "turn-2" if expected["uses_turn_id_fallback"] else None
    assert mapped.get("params", {}).get("turnId") == expected_turn_id


@pytest.mark.asyncio
async def test_client_initialize_and_session_roundtrip(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        initialize = await client.start()
        status = await client.request("fixture/status", {})
        created = await client.create_session(
            cwd=str(tmp_path), title="Fixture Session"
        )
        loaded = await client.load_session(created.session_id)
        listed = await client.list_sessions()

        assert initialize.server_name == "fake-hermes"
        assert status["initialized"] is True
        assert status["initializedNotification"] is True
        assert status["lastOfficialPromptParams"] is None
        assert created.session_id == loaded.session_id
        assert [session.session_id for session in listed] == [created.session_id]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_initialize_failure_surfaces_as_initialization_error(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("initialize_error"), cwd=tmp_path)
    try:
        with pytest.raises(ACPInitializationError, match="initialize failed"):
            await client.start()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_supports_official_acp_session_and_prompt_flow(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        initialize = await client.start()
        created = await client.create_session(cwd=str(tmp_path))
        loaded = await client.load_session(created.session_id)
        listed = await client.list_sessions()
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        events = [event async for event in handle.events()]
        result = await handle.wait()

        assert initialize.server_name == "fake-hermes"
        assert created.session_id == loaded.session_id
        assert [session.session_id for session in listed] == [created.session_id]
        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert [event.kind for event in events] == [
            "turn_started",
            "progress",
            "output_delta",
            "turn_terminal",
        ]
        assert any(
            getattr(event, "method", None) == "session/update"
            and getattr(event, "turn_id", None) == handle.turn_id
            for event in events
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_accepts_official_load_session_empty_object_result(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official_empty_load_result"), cwd=tmp_path)
    try:
        created = await client.create_session(cwd=str(tmp_path))
        loaded = await client.load_session(created.session_id)

        assert loaded.session_id == created.session_id
        assert loaded.raw == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_rejects_official_load_session_null_result(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official_missing_load_result"), cwd=tmp_path)
    try:
        await client.start()
        with pytest.raises(
            ACPMissingSessionError, match="session not found: missing-session"
        ):
            await client.load_session("missing-session")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_prompt_stays_non_terminal_until_request_returns(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official_prompt_hang"), cwd=tmp_path)
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        for _ in range(20):
            if len(handle.snapshot_events()) >= 3:
                break
            await asyncio.sleep(0.01)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(handle.wait(), timeout=0.1)

        assert [event.kind for event in handle.snapshot_events()] == [
            "turn_started",
            "progress",
            "output_delta",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_prompt_hang_tracks_last_session_update_state(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official_prompt_hang"), cwd=tmp_path)
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        for _ in range(20):
            state = client._prompts.get(handle.turn_id)
            if (
                state is not None
                and state.last_session_update_kind == "agent_message_chunk"
            ):
                break
            await asyncio.sleep(0.01)

        state = client._prompts[handle.turn_id]

        assert client._session_active_turns[created.session_id] == handle.turn_id
        assert state.last_session_update_kind == "agent_message_chunk"
        assert state.last_session_update_excerpt == "fixture reply"
        assert state.last_session_update_content_kind == "dict"
        assert state.last_session_update_part_types == ()
        assert state.last_session_update_text_length == len("fixture reply")
        assert state.last_session_update_at is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_terminal_event_can_complete_before_request_returns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = ACPClient(
        fixture_command("official_terminal_before_return"),
        cwd=tmp_path,
    )
    try:
        caplog.set_level("INFO")
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        result = await asyncio.wait_for(handle.wait(), timeout=0.4)
        state = client._prompts[handle.turn_id]
        assert state.request_task is not None
        await asyncio.wait_for(state.request_task, timeout=0.4)
        payloads = [json.loads(record.getMessage()) for record in caplog.records]

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert [event.kind for event in handle.snapshot_events()] == [
            "turn_started",
            "progress",
            "output_delta",
            "turn_terminal",
        ]
        assert any(
            payload.get("event") == "acp.prompt.terminal_recorded"
            and payload.get("completion_source") == "terminal_event"
            for payload in payloads
        )
        assert any(
            payload.get("event") == "acp.prompt.request_returned"
            and payload.get("completion_source") == "prompt_return"
            for payload in payloads
        )
        assert any(
            payload.get("last_runtime_method") == "prompt/completed"
            for payload in payloads
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_terminal_without_turn_id_can_complete_before_request_returns(
    tmp_path: Path,
) -> None:
    client = ACPClient(
        fixture_command("official_terminal_without_turn_id"),
        cwd=tmp_path,
    )
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        result = await asyncio.wait_for(handle.wait(), timeout=0.4)
        state = client._prompts[handle.turn_id]

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert state.request_task is not None
        await asyncio.wait_for(state.request_task, timeout=0.4)
        assert [event.kind for event in handle.snapshot_events()] == [
            "turn_started",
            "progress",
            "output_delta",
            "turn_terminal",
        ]
        assert handle.snapshot_events()[-1].turn_id == handle.turn_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_session_status_idle_can_complete_before_request_returns(
    tmp_path: Path,
) -> None:
    client = ACPClient(
        fixture_command("official_session_status_idle_before_return"),
        cwd=tmp_path,
    )
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        result = await asyncio.wait_for(handle.wait(), timeout=0.4)
        state = client._prompts[handle.turn_id]

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert state.request_task is not None
        await asyncio.wait_for(state.request_task, timeout=0.4)
        assert [event.method for event in handle.snapshot_events()] == [
            "prompt/started",
            "session/update",
            "session/update",
            "session.status",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_prompt_sends_message_id_and_replays_server_turn_alias(
    tmp_path: Path,
) -> None:
    client = ACPClient(
        fixture_command("official_server_assigned_turn_id_before_return"),
        cwd=tmp_path,
    )
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        result = await asyncio.wait_for(handle.wait(), timeout=0.4)
        status = await client.request("fixture/status", {})

        assert status["lastOfficialPromptParams"]["messageId"] == handle.turn_id
        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert any(
            getattr(event, "kind", None) == "turn_terminal"
            and getattr(event, "turn_id", None) == "server-turn-1"
            for event in handle.snapshot_events()
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_request_can_return_after_terminal_event(
    tmp_path: Path,
) -> None:
    client = ACPClient(
        fixture_command("official_request_return_after_terminal"),
        cwd=tmp_path,
    )
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        result = await asyncio.wait_for(handle.wait(), timeout=0.4)
        state = client._prompts[handle.turn_id]

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert state.request_task is not None
        assert state.request_task.done() is False

        await asyncio.wait_for(state.request_task, timeout=0.5)
        assert [event.kind for event in handle.snapshot_events()] == [
            "turn_started",
            "progress",
            "output_delta",
            "turn_terminal",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_official_terminal_event_can_complete_without_request_return(
    tmp_path: Path,
) -> None:
    client = ACPClient(
        fixture_command("official_terminal_without_request_return"),
        cwd=tmp_path,
    )
    try:
        created = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        result = await asyncio.wait_for(handle.wait(), timeout=0.4)
        state = client._prompts[handle.turn_id]

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert state.request_task is not None
        assert state.request_task.done() is False
        assert any(
            method == "session/prompt" for method in client._pending_methods.values()
        )
        assert [event.kind for event in handle.snapshot_events()] == [
            "turn_started",
            "progress",
            "output_delta",
            "turn_terminal",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_logs_official_prompt_lifecycle_trace(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        caplog.set_level("INFO")
        created = await client.create_session(cwd=str(tmp_path))
        await client.load_session(created.session_id)
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        await handle.wait()

        assert "acp.client.initialized" in caplog.text
        assert "acp.session.new" in caplog.text
        assert "acp.session.load" in caplog.text
        assert "acp.prompt.started" in caplog.text
        assert "acp.prompt.session_update" in caplog.text
        assert "acp.prompt.request_returned" in caplog.text
        assert f'"session_id":"{created.session_id}"' in caplog.text
        assert f'"turn_id":"{handle.turn_id}"' in caplog.text
        assert '"completion_source":"prompt_return"' in caplog.text
        assert '"last_runtime_method":"' in caplog.text
        assert '"last_session_update_kind":"agent_message_chunk"' in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_prompt_streams_updates_and_calls_permission_hook(
    tmp_path: Path,
) -> None:
    seen_permissions: list[ACPPermissionRequestEvent] = []

    async def permission_handler(event: ACPPermissionRequestEvent) -> str:
        seen_permissions.append(event)
        return "accept"

    client = ACPClient(
        fixture_command("official"),
        cwd=tmp_path,
        permission_handler=permission_handler,
    )
    try:
        session = await client.create_session(
            cwd=str(tmp_path), title="Fixture Session"
        )
        handle = await client.start_prompt(session.session_id, "needs permission")
        events = [event async for event in handle.events()]
        result = await handle.wait()

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
        assert [event.kind for event in events] == [
            "turn_started",
            "progress",
            "permission_requested",
            "output_delta",
            "turn_terminal",
        ]
        assert len(seen_permissions) == 1
        assert seen_permissions[0].request_id == "perm-1"
        assert seen_permissions[0].turn_id == handle.turn_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_can_deny_permission_requests(tmp_path: Path) -> None:
    async def permission_handler(_event: ACPPermissionRequestEvent) -> str:
        return "decline"

    client = ACPClient(
        fixture_command("official"),
        cwd=tmp_path,
        permission_handler=permission_handler,
    )
    try:
        session = await client.create_session(
            cwd=str(tmp_path), title="Fixture Session"
        )
        handle = await client.start_prompt(session.session_id, "needs permission")
        result = await handle.wait()

        assert result.status == "failed"
        assert result.error_message == "permission denied"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_can_cancel_inflight_prompt(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(session.session_id, "cancel me")
        await client.cancel_prompt(session.session_id, handle.turn_id)
        result = await handle.wait()

        assert result.status == "cancelled"
        assert result.final_output == ""
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_optional_custom_method_missing_returns_none(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        echoed = await client.call_optional("custom/echo", {"value": "ok"})
        missing = await client.call_optional("custom/missing", {"value": "noop"})

        assert echoed == {"echo": {"value": "ok"}}
        assert missing is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_reports_subprocess_crash_during_prompt(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(session.session_id, "crash")

        with pytest.raises(ACPProcessCrashedError, match="exited with code 17"):
            await handle.wait()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_ignores_known_cli_noise_on_stdout(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(session.session_id, "stdout noise")
        result = await handle.wait()

        assert result.status == "completed"
        assert result.final_output == "fixture reply"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_rejects_unclassified_non_json_stdout(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(session.session_id, "stdout invalid")

        with pytest.raises(
            ACPProtocolError, match="ACP subprocess emitted invalid JSON"
        ):
            await handle.wait()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_rejects_bracketed_json_like_stdout(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(
            session.session_id, "stdout invalid bracketed"
        )

        with pytest.raises(
            ACPProtocolError, match="ACP subprocess emitted invalid JSON"
        ):
            await handle.wait()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_logs_background_permission_notification_task_failures(
    tmp_path: Path,
) -> None:
    async def permission_handler(_event: ACPPermissionRequestEvent) -> str:
        return "accept"

    failures: list[str] = []

    client = ACPClient(
        fixture_command("official"),
        cwd=tmp_path,
        permission_handler=permission_handler,
    )

    async def boom(_event, _decision):
        raise RuntimeError("background task boom")

    def capture(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except Exception as exc:
            failures.append(str(exc))

    try:
        client._respond_to_permission_notification = boom  # type: ignore[assignment]
        client._log_background_task_result = capture  # type: ignore[assignment]
        await client._dispatch_message(
            {
                "method": "permission/requested",
                "params": {
                    "sessionId": "session-1",
                    "turnId": "turn-1",
                    "requestId": "req-1",
                    "description": "Need approval",
                    "options": [],
                },
            }
        )
        await asyncio.sleep(0.05)

        assert failures == ["background task boom"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_fork_session_creates_new_session(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        await client.start()
        created = await client.create_session(cwd=str(tmp_path), title="Original")
        forked = await client.fork_session(created.session_id, title="Forked Session")

        assert isinstance(forked, ACPSessionForkResult)
        assert forked.supported is True
        assert forked.session_id is not None
        assert forked.session_id != created.session_id
        assert forked.title == "Forked Session"

        listed = await client.list_sessions()
        session_ids = [s.session_id for s in listed]
        assert created.session_id in session_ids
        assert forked.session_id in session_ids
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_fork_session_unsupported_returns_unsupported(
    tmp_path: Path,
) -> None:
    result = ACPSessionForkResult.from_optional_response(None)
    assert result.supported is False
    assert result.session_id is None


@pytest.mark.asyncio
async def test_client_set_session_model_returns_supported(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        await client.start()
        created = await client.create_session(cwd=str(tmp_path))
        result = await client.set_session_model(
            created.session_id, "anthropic/claude-opus"
        )

        assert isinstance(result, ACPSetModelResult)
        assert result.supported is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_set_session_model_unsupported_returns_unsupported(
    tmp_path: Path,
) -> None:
    result = ACPSetModelResult.from_optional_response(None)
    assert result.supported is False


@pytest.mark.asyncio
async def test_client_set_session_mode_returns_supported(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        await client.start()
        created = await client.create_session(cwd=str(tmp_path))
        result = await client.set_session_mode(created.session_id, "code")

        assert isinstance(result, ACPSetModeResult)
        assert result.supported is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_set_session_mode_unsupported_returns_unsupported(
    tmp_path: Path,
) -> None:
    result = ACPSetModeResult.from_optional_response(None)
    assert result.supported is False


@pytest.mark.asyncio
async def test_client_advertised_commands_extracts_from_capabilities(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("official"), cwd=tmp_path)
    try:
        await client.start()
        commands = client.advertised_commands

        assert isinstance(commands, list)
    finally:
        await client.close()


def test_extract_advertised_commands_from_various_payloads() -> None:
    assert extract_advertised_commands({}) == []
    assert extract_advertised_commands(None) == []

    commands = extract_advertised_commands(
        {
            "commands": [
                {"name": "/help", "description": "Show help"},
                {"name": "/reset", "description": "Reset thread"},
            ]
        }
    )
    assert len(commands) == 2
    assert commands[0].name == "/help"
    assert commands[0].description == "Show help"
    assert commands[1].name == "/reset"

    commands = extract_advertised_commands(
        {"slashCommands": [{"command": "/status", "desc": "Status"}]}
    )
    assert len(commands) == 1
    assert commands[0].name == "/status"
    assert commands[0].description == "Status"

    commands = extract_advertised_commands(
        {"sessionCapabilities": {"commands": [{"name": "/mode"}]}}
    )
    assert len(commands) == 1
    assert commands[0].name == "/mode"

    commands = extract_advertised_commands({"commands": [{"no_name": True}]})
    assert len(commands) == 0


def test_extract_session_capabilities_from_various_payloads() -> None:
    caps = extract_session_capabilities({})
    assert caps.fork is False
    assert caps.set_model is False
    assert caps.set_mode is False
    assert caps.list_sessions is False

    caps = extract_session_capabilities(
        {
            "sessionCapabilities": {
                "list": {},
                "fork": {},
                "setModel": {},
                "setMode": {},
            }
        }
    )
    assert caps.list_sessions is True
    assert caps.fork is True
    assert caps.set_model is True
    assert caps.set_mode is True


class TestClientLifecycleDelegation:
    def test_client_imports_shared_turn_id_fallback(self) -> None:
        from codex_autorunner.agents.acp.client import (
            _should_map_missing_turn_id,
        )

        for method in _SESSION_TURN_ID_FALLBACK_METHODS:
            assert _should_map_missing_turn_id(method, {})

    def test_client_fallback_matches_shared_lifecycle(self) -> None:
        from codex_autorunner.agents.acp.client import (
            _should_map_missing_turn_id,
        )

        for method in _TERMINAL_METHODS:
            assert _should_map_missing_turn_id(
                method, {}
            ) == _shared_should_map_missing_turn_id(method, {})

        for method in (
            "prompt/started",
            "turn/started",
            "permission/requested",
            "token/usage",
        ):
            assert _should_map_missing_turn_id(
                method, {}
            ) == _shared_should_map_missing_turn_id(method, {})

    def test_client_idle_terminal_not_mapped_without_active_turn(self) -> None:
        client = ACPClient(fixture_command("official"))
        for method in _IDLE_TERMINAL_METHODS:
            mapped = client._message_with_mapped_turn_id(
                {"method": method, "params": {"sessionId": "session-1"}}
            )
            assert mapped.get("params", {}).get("turnId") is None

    @pytest.mark.parametrize(
        ("case"),
        load_acp_lifecycle_corpus(),
        ids=[case["name"] for case in load_acp_lifecycle_corpus()],
    )
    def test_client_turn_id_mapping_consistent_with_shared_lifecycle(
        self,
        case: dict[str, object],
    ) -> None:
        client = ACPClient(fixture_command("official"))
        client._session_active_turns["session-1"] = "turn-active"
        raw = dict(case["raw"])
        expected = dict(case["expected"])

        mapped = client._message_with_mapped_turn_id(raw)

        params = mapped.get("params", {})
        if expected["uses_turn_id_fallback"]:
            assert params.get("turnId") == "turn-active"
        else:
            assert params.get("turnId") is None
