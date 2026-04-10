from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from codex_autorunner.agents.acp import (
    ACPClient,
    ACPInitializationError,
    ACPMethodNotFoundError,
    ACPPermissionRequestEvent,
)
from codex_autorunner.agents.acp.errors import (
    ACPProcessCrashedError,
    ACPProtocolError,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"
pytestmark = pytest.mark.slow


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


def test_client_does_not_map_prompt_terminal_notifications_without_turn_id() -> None:
    client = ACPClient(fixture_command("basic"))
    client._session_active_turns["session-1"] = "turn-2"

    message = client._message_with_mapped_turn_id(
        {
            "method": "prompt/completed",
            "params": {
                "sessionId": "session-1",
                "status": "completed",
            },
        }
    )

    assert "turnId" not in message["params"]


@pytest.mark.asyncio
async def test_client_primes_prompt_state_with_fallback_session_id() -> None:
    client = ACPClient(fixture_command("basic"))
    try:
        client._prime_prompt_state_from_start_result(
            {"turnId": "turn-1"},
            fallback_session_id="session-1",
        )

        assert client._session_active_turns == {"session-1": "turn-1"}
        assert "turn-1" in client._prompts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_initialize_and_session_roundtrip(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
    try:
        initialize = await client.start()
        status = await client.request("fixture/status", {})
        created = await client.create_session(
            cwd=str(tmp_path), title="Fixture Session"
        )
        loaded = await client.load_session(created.session_id)
        listed = await client.list_sessions()

        assert initialize.server_name == "fake-acp"
        assert status == {
            "initialized": True,
            "initializedNotification": True,
        }
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
        with pytest.raises(ACPMethodNotFoundError, match="session/list"):
            await client.list_sessions()
        handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
        events = [event async for event in handle.events()]
        result = await handle.wait()

        assert initialize.server_name == "fake-hermes"
        assert created.session_id == loaded.session_id
        assert result.status == "completed"
        assert result.final_output == "OK"
        assert [event.kind for event in events] == [
            "turn_started",
            "progress",
            "output_delta",
            "turn_terminal",
        ]
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
        fixture_command("basic"),
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
            "output_delta",
            "permission_requested",
            "output_delta",
            "turn_terminal",
        ]
        assert len(seen_permissions) == 1
        assert seen_permissions[0].request_id == "perm-1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_can_deny_permission_requests(tmp_path: Path) -> None:
    async def permission_handler(_event: ACPPermissionRequestEvent) -> str:
        return "decline"

    client = ACPClient(
        fixture_command("basic"),
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
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(session.session_id, "cancel me")
        await client.cancel_prompt(session.session_id, handle.turn_id)
        result = await handle.wait()

        assert result.status == "cancelled"
        assert result.final_output == "fixture "
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_optional_custom_method_missing_returns_none(
    tmp_path: Path,
) -> None:
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
    try:
        echoed = await client.call_optional("custom/echo", {"value": "ok"})
        missing = await client.call_optional("custom/missing", {"value": "noop"})

        assert echoed == {"echo": {"value": "ok"}}
        assert missing is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_reports_subprocess_crash_during_prompt(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
    try:
        session = await client.create_session(cwd=str(tmp_path))
        handle = await client.start_prompt(session.session_id, "crash")

        with pytest.raises(ACPProcessCrashedError, match="exited with code 17"):
            await handle.wait()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_ignores_known_cli_noise_on_stdout(tmp_path: Path) -> None:
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
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
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
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
    client = ACPClient(fixture_command("basic"), cwd=tmp_path)
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
        fixture_command("basic"),
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
