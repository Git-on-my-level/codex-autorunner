import asyncio
import sys
from pathlib import Path

import pytest

from codex_autorunner.integrations.app_server import client as app_server_client
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerClient,
    _extract_agent_message_text,
)

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
async def test_turn_error_notification(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("turn_error_no_agent"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "failed"
        assert result.agent_messages == []
        assert result.errors == ["Auth required"]
    finally:
        await client.close()


def test_extract_agent_message_text_supports_content_list() -> None:
    item = {
        "type": "agentMessage",
        "content": [
            {"type": "output_text", "text": "hello"},
            {"type": "output_text", "text": " world"},
        ],
    }
    assert _extract_agent_message_text(item) == "hello world"


@pytest.mark.anyio
async def test_review_message_dedupes_review_text(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("review_duplicate"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.review_start(thread["id"], target={"type": "custom"})
        result = await handle.wait()
        assert result.status == "completed"
        assert result.agent_messages == ["fixture reply"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_thread_list_includes_params(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("thread_list_requires_params"), cwd=tmp_path
    )
    try:
        threads = await client.thread_list()
        assert isinstance(threads, list)
        assert threads
    finally:
        await client.close()


@pytest.mark.anyio
async def test_thread_list_normalizes_data_shape(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("thread_list_data_shape"), cwd=tmp_path
    )
    try:
        threads = await client.thread_list()
        assert isinstance(threads, dict)
        assert isinstance(threads.get("threads"), list)
        assert threads["threads"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_start_normalizes_sandbox_policy(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("sandbox_policy_check"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(
            thread["id"], "hi", sandbox_policy="danger-full-access"
        )
        result = await handle.wait()
        assert result.status == "completed"
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["thread_id_key", "thread_id_snake"])
async def test_thread_start_accepts_alt_thread_id_keys(
    tmp_path: Path, scenario: str
) -> None:
    client = CodexAppServerClient(fixture_command(scenario), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        assert isinstance(thread.get("id"), str)
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
        await client.turn_interrupt(handle.turn_id, thread_id=handle.thread_id)
        result = await handle.wait()
        assert result.status == "interrupted"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_completed_via_resume_when_completion_missing(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("missing_turn_completed"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=0.5,
        turn_stall_poll_interval_seconds=0.1,
        turn_stall_recovery_min_interval_seconds=0.0,
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait(timeout=5)
        assert result.status == "completed"
        assert result.agent_messages == ["recovered reply"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_restart_after_crash(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("crash"), cwd=tmp_path, auto_restart=True
    )
    try:
        await client.request("fixture/crash")
        await client.wait_for_disconnect(timeout=1)
        result = await client.request("fixture/echo", {"value": 42})
        assert result["value"] == 42
    finally:
        await client.close()


@pytest.mark.anyio
async def test_large_response_line(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        large_value = "x" * (256 * 1024)
        result = await client.request("fixture/echo", {"value": large_value})
        assert result["value"] == large_value
    finally:
        await client.close()


@pytest.mark.anyio
async def test_response_without_trailing_newline(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        auto_restart=False,
    )
    try:
        result = await client.request("fixture/echo_no_newline", {"value": "final"})
        assert result["value"] == "final"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_oversize_line_drops_and_preserves_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server_client, "_MAX_MESSAGE_BYTES", 128)
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        result = await client.request("fixture/oversize_drop", {"value": "ok"})
        assert result["value"] == "ok"
    finally:
        await client.close()
