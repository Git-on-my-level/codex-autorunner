from __future__ import annotations

from pathlib import Path

import pytest

from tests.chat_surface_lab.backend_runtime import (
    ACPFixtureRuntime,
    CodexAppServerFixtureRuntime,
    OpenCodeFixtureRuntime,
)


def _workspace_root(tmp_path: Path, name: str) -> Path:
    workspace = tmp_path / name
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir(exist_ok=True)
    return workspace


async def _start_runtime(runtime, workspace_root: Path):
    await runtime.start(workspace_root)
    ready = await runtime.wait_for_event(lambda event: event.kind == "runtime.ready")
    return ready


async def _shutdown_runtime(runtime) -> None:
    await runtime.shutdown()
    closed = await runtime.wait_for_event(lambda event: event.kind == "runtime.closed")
    assert closed.backend == runtime.backend_name


@pytest.mark.anyio
async def test_codex_app_server_runtime_smoke_streams_normalized_turn_events(
    tmp_path: Path,
) -> None:
    runtime = CodexAppServerFixtureRuntime(scenario="basic")
    await _start_runtime(runtime, _workspace_root(tmp_path, "app-server-basic"))
    try:
        conversation_id = await runtime.create_conversation()
        conversation = await runtime.wait_for_event(
            lambda event: event.kind == "conversation.started"
        )
        assert conversation.conversation_id == conversation_id

        turn_id = await runtime.start_turn(conversation_id, "echo hello world")
        started = await runtime.wait_for_event(
            lambda event: event.kind == "turn.started"
            and event.turn_id == turn_id
            and event.conversation_id == conversation_id
        )
        assert started.turn_id == turn_id

        output = await runtime.wait_for_event(
            lambda event: event.kind == "turn.output" and event.turn_id == turn_id
        )
        assert output.text == "fixture reply"

        terminal = await runtime.wait_for_event(
            lambda event: event.kind == "turn.terminal" and event.turn_id == turn_id
        )
        assert terminal.status == "completed"
    finally:
        await _shutdown_runtime(runtime)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("runtime_factory", "workspace_name", "prompt"),
    [
        (
            lambda: CodexAppServerFixtureRuntime(scenario="approval"),
            "app-server-approval",
            "needs permission",
        ),
        (
            lambda: ACPFixtureRuntime(scenario="official"),
            "acp-approval",
            "needs permission",
        ),
    ],
)
async def test_runtime_contract_normalizes_approval_controls(
    tmp_path: Path,
    runtime_factory,
    workspace_name: str,
    prompt: str,
) -> None:
    runtime = runtime_factory()
    await _start_runtime(runtime, _workspace_root(tmp_path, workspace_name))
    try:
        conversation_id = await runtime.create_conversation()
        turn_id = await runtime.start_turn(conversation_id, prompt)

        approval = await runtime.wait_for_event(
            lambda event: event.kind == "control.approval_requested"
            and event.turn_id == turn_id
        )
        assert approval.control_id

        await runtime.respond_to_control(approval.control_id, "approve")
        terminal = await runtime.wait_for_event(
            lambda event: event.kind == "turn.terminal" and event.turn_id == turn_id,
            timeout=4.0,
        )
        assert terminal.status == "completed"
    finally:
        await _shutdown_runtime(runtime)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("runtime_factory", "workspace_name", "prompt"),
    [
        (
            lambda: CodexAppServerFixtureRuntime(scenario="interrupt"),
            "app-server-interrupt",
            "cancel me",
        ),
        (
            lambda: ACPFixtureRuntime(scenario="official"),
            "acp-interrupt",
            "cancel me",
        ),
    ],
)
async def test_runtime_contract_normalizes_interrupt_status(
    tmp_path: Path,
    runtime_factory,
    workspace_name: str,
    prompt: str,
) -> None:
    runtime = runtime_factory()
    await _start_runtime(runtime, _workspace_root(tmp_path, workspace_name))
    try:
        conversation_id = await runtime.create_conversation()
        turn_id = await runtime.start_turn(conversation_id, prompt)
        await runtime.wait_for_event(
            lambda event: event.kind == "turn.started" and event.turn_id == turn_id
        )

        await runtime.interrupt(conversation_id, turn_id)
        terminal = await runtime.wait_for_event(
            lambda event: event.kind == "turn.terminal" and event.turn_id == turn_id,
            timeout=4.0,
        )
        assert terminal.status == "interrupted"
    finally:
        await _shutdown_runtime(runtime)


@pytest.mark.anyio
async def test_opencode_runtime_smoke_starts_fixture_server(
    tmp_path: Path,
) -> None:
    runtime = OpenCodeFixtureRuntime()
    ready = await _start_runtime(runtime, _workspace_root(tmp_path, "opencode-smoke"))
    try:
        assert ready.backend == "opencode"
        assert ready.payload["health"] == {"status": "ok"}
        assert ready.payload["supports_global_endpoints"] is True
        assert runtime.capabilities.can_create_conversation is False
        assert runtime.capabilities.can_interrupt is False
    finally:
        await _shutdown_runtime(runtime)
