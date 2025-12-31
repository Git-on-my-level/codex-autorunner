import asyncio
import sys
from pathlib import Path

import pytest

from codex_autorunner.app_server_client import CodexAppServerClient


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_server_fixture.py"


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


@pytest.mark.anyio
async def test_handshake_and_status(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        status = await client.request("fixture/status")
        assert status["initialized"] is True
        assert status["initializedNotification"] is True
    finally:
        await client.close()


@pytest.mark.anyio
async def test_request_response_out_of_order(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        slow_task = asyncio.create_task(
            client.request("fixture/slow", {"value": "slow"})
        )
        fast_task = asyncio.create_task(
            client.request("fixture/fast", {"value": "fast"})
        )
        assert await fast_task == {"value": "fast"}
        assert await slow_task == {"value": "slow"}
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_completion_and_agent_message(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "completed"
        assert result.agent_messages == ["fixture reply"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_approval_flow(tmp_path: Path) -> None:
    approvals: list[dict] = []

    async def approve(request: dict) -> str:
        approvals.append(request)
        return "accept"

    client = CodexAppServerClient(
        fixture_command("approval"),
        cwd=tmp_path,
        approval_handler=approve,
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert approvals
        assert result.status == "completed"
        assert any(
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("approvalDecision") == "accept"
            for event in result.raw_events
        )
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_interrupt(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("interrupt"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        await client.turn_interrupt(handle.turn_id)
        result = await handle.wait()
        assert result.status == "interrupted"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_restart_after_crash(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("crash"), cwd=tmp_path)
    try:
        await client.request("fixture/crash")
        await client.wait_for_disconnect(timeout=1)
        result = await client.request("fixture/echo", {"value": 42})
        assert result["value"] == 42
    finally:
        await client.close()
