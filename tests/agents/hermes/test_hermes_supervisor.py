from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from codex_autorunner.agents.acp.errors import (
    ACPInitializationError,
    ACPProcessCrashedError,
)
from codex_autorunner.agents.hermes.supervisor import (
    HermesSupervisor,
    HermesSupervisorError,
)

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
async def test_hermes_supervisor_bridges_permission_requests_and_allows(
    tmp_path: Path,
) -> None:
    seen_requests: list[dict[str, object]] = []

    async def approval_handler(request: dict[str, object]) -> str:
        seen_requests.append(request)
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=0.1,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )

        events = await _collect_events(
            supervisor, tmp_path, session.session_id, turn_id
        )
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert seen_requests
        assert seen_requests[0]["method"] == "item/commandExecution/requestApproval"
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "prompt/progress",
            "session/request_permission",
            "permission/decision",
            "prompt/progress",
            "prompt/completed",
        ]
        assert [event.get("method") for event in result.raw_events] == [
            "prompt/started",
            "prompt/progress",
            "session/request_permission",
            "permission/decision",
            "prompt/progress",
            "prompt/completed",
        ]
        assert result.raw_events[3]["params"]["decision"] == "accept"
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_auto_accepts_when_approval_mode_never(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(
        fixture_command("basic"),
        default_approval_decision="cancel",
        approval_timeout_seconds=0.1,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="never",
        )
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "completed"
        assert any(
            event.get("method") == "permission/decision"
            and event.get("params", {}).get("decision") == "accept"
            and event.get("params", {}).get("reason") == "policy_auto_accept"
            for event in result.raw_events
        )
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_can_deny_permission_requests(
    tmp_path: Path,
) -> None:
    async def approval_handler(_request: dict[str, object]) -> str:
        return "decline"

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=0.1,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "failed"
        assert result.errors == ["permission denied"]
        assert result.raw_events[3]["method"] == "permission/decision"
        assert result.raw_events[3]["params"]["decision"] == "decline"
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_times_out_pending_permission_requests(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    async def approval_handler(_request: dict[str, object]) -> str:
        await gate.wait()
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=0.05,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "cancelled"
        assert any(
            event.get("method") == "permission/decision"
            and event.get("params", {}).get("reason") == "timeout"
            for event in result.raw_events
        )
    finally:
        gate.set()
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
async def test_hermes_supervisor_interrupt_cancels_pending_permission_wait(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    async def approval_handler(_request: dict[str, object]) -> str:
        await gate.wait()
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=5.0,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )
        await asyncio.sleep(0.05)
        await supervisor.interrupt_turn(tmp_path, session.session_id, turn_id)
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "cancelled"
        assert any(
            event.get("method") == "permission/decision"
            and event.get("params", {}).get("reason") == "cancelled"
            for event in result.raw_events
        )
    finally:
        gate.set()
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_replacing_turn_cancels_previous_pending_approval(
    tmp_path: Path,
) -> None:
    approval_started = asyncio.Event()
    approval_cancelled = asyncio.Event()
    approval_gate = asyncio.Event()

    async def approval_handler(_request: dict[str, object]) -> str:
        approval_started.set()
        try:
            await approval_gate.wait()
        except asyncio.CancelledError:
            approval_cancelled.set()
            raise
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=5.0,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        first_turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )
        await asyncio.wait_for(approval_started.wait(), timeout=1.0)

        second_turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "hello again",
            approval_mode="never",
        )

        assert second_turn_id != first_turn_id
        await asyncio.wait_for(approval_cancelled.wait(), timeout=1.0)
        second_result = await supervisor.wait_for_turn(
            tmp_path,
            session.session_id,
            second_turn_id,
        )

        with pytest.raises(HermesSupervisorError, match="Unknown Hermes turn"):
            await supervisor.wait_for_turn(tmp_path, session.session_id, first_turn_id)
        assert second_result.status == "completed"
    finally:
        approval_gate.set()
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_close_workspace_retires_pending_turn_state(
    tmp_path: Path,
) -> None:
    approval_started = asyncio.Event()
    approval_cancelled = asyncio.Event()
    approval_gate = asyncio.Event()

    async def approval_handler(_request: dict[str, object]) -> str:
        approval_started.set()
        try:
            await approval_gate.wait()
        except asyncio.CancelledError:
            approval_cancelled.set()
            raise
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=5.0,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )
        await asyncio.wait_for(approval_started.wait(), timeout=1.0)

        await supervisor.close_workspace(tmp_path)

        await asyncio.wait_for(approval_cancelled.wait(), timeout=1.0)
        with pytest.raises(HermesSupervisorError, match="Unknown Hermes turn"):
            await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)
    finally:
        approval_gate.set()
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_propagates_approval_handler_exception(
    tmp_path: Path,
) -> None:
    async def approval_handler(_request: dict[str, object]) -> str:
        raise RuntimeError("approval handler boom")

    supervisor = HermesSupervisor(
        fixture_command("basic"),
        approval_handler=approval_handler,
        default_approval_decision="cancel",
        approval_timeout_seconds=5.0,
    )
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "needs permission",
            approval_mode="on-request",
        )
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "cancelled"
        assert any(
            event.get("method") == "permission/decision"
            and event.get("params", {}).get("reason") == "handler_error"
            for event in result.raw_events
        )
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_propagates_initialize_error(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("initialize_error"))
    try:
        with pytest.raises(ACPInitializationError, match="initialize failed"):
            await supervisor.ensure_ready(tmp_path)
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_propagates_subprocess_crash_during_wait(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("crash"))
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(tmp_path, session.session_id, "crash")

        with pytest.raises(ACPProcessCrashedError, match="exited with code 17"):
            await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)
    finally:
        await supervisor.close_all()


@pytest.mark.asyncio
async def test_hermes_supervisor_rejects_unknown_turn_lookup(tmp_path: Path) -> None:
    supervisor = HermesSupervisor(fixture_command("basic"))
    try:
        session = await supervisor.create_session(tmp_path)

        with pytest.raises(
            HermesSupervisorError, match="No active Hermes turn tracked"
        ):
            await supervisor.interrupt_turn(tmp_path, session.session_id, None)
    finally:
        await supervisor.close_all()
