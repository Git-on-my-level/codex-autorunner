from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codex_autorunner.agents.acp import (
    ACPClient,
    ACPInitializationError,
    ACPPermissionRequestEvent,
)
from codex_autorunner.agents.acp.errors import ACPProcessCrashedError

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


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
async def test_client_prompt_streams_updates_and_calls_permission_hook(
    tmp_path: Path,
) -> None:
    seen_permissions: list[ACPPermissionRequestEvent] = []

    async def permission_handler(event: ACPPermissionRequestEvent) -> None:
        seen_permissions.append(event)

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
