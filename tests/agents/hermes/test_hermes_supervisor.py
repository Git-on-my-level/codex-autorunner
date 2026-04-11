from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.agents.acp.errors import (
    ACPInitializationError,
    ACPProcessCrashedError,
)
from codex_autorunner.agents.acp.events import normalize_notification
from codex_autorunner.agents.hermes.supervisor import (
    HermesSupervisor,
    HermesSupervisorError,
    _should_close_turn_buffer,
    build_hermes_supervisor_from_config,
    hermes_runtime_preflight,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


class _HermesPreflightConfig:
    def agent_binary(self, agent_id: str) -> str:
        assert agent_id == "hermes"
        return "hermes"


class _HermesAliasConfig:
    def agent_binary(self, agent_id: str, *, profile: str | None = None) -> str:
        assert profile is None
        if agent_id == "hermes":
            return "hermes"
        if agent_id == "hermes-m4-pma":
            return "hermes-m4-pma"
        raise AssertionError(f"unexpected agent_id: {agent_id}")


class _HermesCustomAliasConfig:
    def agent_binary(self, agent_id: str, *, profile: str | None = None) -> str:
        assert profile is None
        if agent_id == "hermes":
            return "hermes"
        if agent_id == "hermes-special":
            return "/opt/hermes/special-launcher"
        raise AssertionError(f"unexpected agent_id: {agent_id}")


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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_session_roundtrip_and_turn_streaming(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("official"))
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
            "session/update",
            "session/update",
            "prompt/completed",
        ]
        assert [event.get("method") for event in result.raw_events] == [
            "prompt/started",
            "session/update",
            "session/update",
            "prompt/completed",
        ]
    finally:
        await supervisor.close_all()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_maps_session_scoped_updates_to_active_turn(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("official"))
    try:
        await supervisor.ensure_ready(tmp_path)
        session = await supervisor.create_session(tmp_path, title="Official mapping")
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "hello from hermes",
        )
        result = await asyncio.wait_for(
            supervisor.wait_for_turn(tmp_path, session.session_id, turn_id),
            timeout=2.0,
        )
        events = await _collect_events(
            supervisor, tmp_path, session.session_id, turn_id
        )

        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert any(
            event.get("method") == "session/update"
            and event.get("params", {}).get("turnId") == turn_id
            for event in events
        )
    finally:
        await supervisor.close_all()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_can_complete_from_idle_status_before_prompt_return(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(
        fixture_command("official_session_status_idle_before_return")
    )
    try:
        await supervisor.ensure_ready(tmp_path)
        session = await supervisor.create_session(tmp_path, title="Official mapping")
        turn_id = await supervisor.start_turn(
            tmp_path,
            session.session_id,
            "hello from hermes",
        )
        result = await asyncio.wait_for(
            supervisor.wait_for_turn(tmp_path, session.session_id, turn_id),
            timeout=2.0,
        )
        events = await _collect_events(
            supervisor, tmp_path, session.session_id, turn_id
        )

        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "session/update",
            "session/update",
            "session.status",
        ]
        assert all(
            event.get("params", {}).get("turnId") == turn_id
            for event in events[1:]
            if isinstance(event.get("params"), dict)
        )
    finally:
        await supervisor.close_all()


def test_hermes_supervisor_shared_lifecycle_fixture_buffer_closing() -> None:
    for case in load_acp_lifecycle_corpus():
        raw = dict(case["raw"])
        expected = dict(case["expected"])
        event = normalize_notification(raw)

        if event.kind != "turn_terminal":
            continue
        assert _should_close_turn_buffer(event) is expected["closes_turn_buffer"]


def test_hermes_runtime_preflight_accepts_plain_acp_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.hermes.supervisor.resolve_executable",
        lambda _name: "/usr/bin/hermes",
    )

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        if cmd == ["hermes", "--version"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="hermes 1.2.3\n",
                stderr="",
            )
        if cmd == ["hermes", "acp", "--help"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="",
                stderr="Usage: hermes acp [OPTIONS]\n  Start the ACP server.\n",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = hermes_runtime_preflight(_HermesPreflightConfig())

    assert result.status == "ready"
    assert result.launch_mode is None
    assert result.version == "hermes 1.2.3"
    assert "Hermes-native durable sessions" in result.message


def test_build_hermes_supervisor_prefers_base_binary_for_alias_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _FakeHermesSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            observed["command"] = list(command)
            observed["kwargs"] = dict(kwargs)

    monkeypatch.setattr(
        "codex_autorunner.agents.hermes.supervisor.HermesSupervisor",
        _FakeHermesSupervisor,
    )

    supervisor = build_hermes_supervisor_from_config(
        _HermesAliasConfig(),
        agent_id="hermes-m4-pma",
    )

    assert supervisor is not None
    assert observed["command"] == [
        "hermes",
        "-p",
        "hermes-m4-pma",
        "acp",
    ]


def test_hermes_runtime_preflight_prefers_base_binary_for_alias_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_commands: list[list[str]] = []

    monkeypatch.setattr(
        "codex_autorunner.agents.hermes.supervisor.resolve_executable",
        lambda _name: "/usr/bin/hermes",
    )

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        observed_commands.append(list(cmd))
        if cmd == ["hermes", "--version"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="hermes 1.2.3\n",
                stderr="",
            )
        if cmd == ["hermes", "-p", "hermes-m4-pma", "acp", "--help"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="",
                stderr="Usage: hermes acp [OPTIONS]\n  Start the ACP server.\n",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = hermes_runtime_preflight(
        _HermesAliasConfig(),
        agent_id="hermes-m4-pma",
    )

    assert observed_commands == [
        ["hermes", "--version"],
        ["hermes", "-p", "hermes-m4-pma", "acp", "--help"],
    ]
    assert result.status == "ready"
    assert result.version == "hermes 1.2.3"


def test_build_hermes_supervisor_preserves_custom_alias_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _FakeHermesSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            observed["command"] = list(command)
            observed["kwargs"] = dict(kwargs)

    monkeypatch.setattr(
        "codex_autorunner.agents.hermes.supervisor.HermesSupervisor",
        _FakeHermesSupervisor,
    )

    supervisor = build_hermes_supervisor_from_config(
        _HermesCustomAliasConfig(),
        agent_id="hermes-special",
    )

    assert supervisor is not None
    assert observed["command"] == [
        "/opt/hermes/special-launcher",
        "acp",
    ]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_bridges_permission_requests_and_allows(
    tmp_path: Path,
) -> None:
    seen_requests: list[dict[str, object]] = []

    async def approval_handler(request: dict[str, object]) -> str:
        seen_requests.append(request)
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("official"),
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
        stream_task = asyncio.create_task(
            _collect_events(supervisor, tmp_path, session.session_id, turn_id)
        )
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)
        events = await stream_task

        assert result.status == "completed"
        assert result.assistant_text == "fixture reply"
        assert seen_requests
        assert seen_requests[0]["method"] == "item/commandExecution/requestApproval"
        assert [event.get("method") for event in events] == [
            "prompt/started",
            "session/update",
            "session/request_permission",
            "permission/decision",
            "session/update",
            "prompt/completed",
        ]
        assert [event.get("method") for event in result.raw_events] == [
            "prompt/started",
            "session/update",
            "session/request_permission",
            "permission/decision",
            "session/update",
            "prompt/completed",
        ]
        assert result.raw_events[3]["params"]["decision"] == "accept"
        assert events[2]["params"]["turnId"] == turn_id
    finally:
        await supervisor.close_all()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_auto_accepts_when_approval_mode_never(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(
        fixture_command("official"),
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_can_deny_permission_requests(
    tmp_path: Path,
) -> None:
    async def approval_handler(_request: dict[str, object]) -> str:
        return "decline"

    supervisor = HermesSupervisor(
        fixture_command("official"),
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_times_out_pending_permission_requests(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    async def approval_handler(_request: dict[str, object]) -> str:
        await gate.wait()
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("official"),
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_can_interrupt_active_turn_without_explicit_turn_id(
    tmp_path: Path,
) -> None:
    supervisor = HermesSupervisor(fixture_command("official"))
    try:
        session = await supervisor.create_session(tmp_path)
        turn_id = await supervisor.start_turn(tmp_path, session.session_id, "cancel me")
        await supervisor.interrupt_turn(tmp_path, session.session_id, None)
        result = await supervisor.wait_for_turn(tmp_path, session.session_id, turn_id)

        assert result.status == "cancelled"
        assert result.assistant_text == ""
    finally:
        await supervisor.close_all()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_interrupt_cancels_pending_permission_wait(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    async def approval_handler(_request: dict[str, object]) -> str:
        await gate.wait()
        return "accept"

    supervisor = HermesSupervisor(
        fixture_command("official"),
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


@pytest.mark.slow
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
        fixture_command("official"),
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


@pytest.mark.slow
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
        fixture_command("official"),
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_propagates_approval_handler_exception(
    tmp_path: Path,
) -> None:
    async def approval_handler(_request: dict[str, object]) -> str:
        raise RuntimeError("approval handler boom")

    supervisor = HermesSupervisor(
        fixture_command("official"),
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hermes_supervisor_rejects_unknown_turn_lookup(tmp_path: Path) -> None:
    supervisor = HermesSupervisor(fixture_command("official"))
    try:
        session = await supervisor.create_session(tmp_path)

        with pytest.raises(
            HermesSupervisorError, match="No active Hermes turn tracked"
        ):
            await supervisor.interrupt_turn(tmp_path, session.session_id, None)
    finally:
        await supervisor.close_all()
