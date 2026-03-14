from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.agents.zeroclaw.supervisor import ZeroClawSupervisor


class _FakeZeroClawClient:
    instances: list["_FakeZeroClawClient"] = []

    def __init__(
        self,
        command,
        *,
        runtime_workspace_root: Path,
        session_state_file: Path,
        logger=None,
        base_env=None,
        launch_provider: str | None = None,
        launch_model: str | None = None,
    ) -> None:
        self.command = list(command)
        self.runtime_workspace_root = runtime_workspace_root
        self.session_state_file = session_state_file
        self.logger = logger
        self.base_env = base_env
        self.launch_provider = launch_provider
        self.launch_model = launch_model
        self.started: list[tuple[str, str | None, str | None]] = []
        self.waited: list[tuple[str, float | None]] = []
        self.streamed: list[str] = []
        self.closed = False
        _FakeZeroClawClient.instances.append(self)

    async def start_turn(
        self,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        if self.launch_provider is not None and provider not in {
            None,
            self.launch_provider,
        }:
            raise AssertionError("provider mismatch")
        if self.launch_model is not None and model not in {None, self.launch_model}:
            raise AssertionError("model mismatch")
        if provider is not None:
            self.launch_provider = provider
        if model is not None:
            self.launch_model = model
        self.started.append((prompt, provider, model))
        return f"turn-{len(self.started)}"

    async def wait_for_turn(
        self, turn_id: str, *, timeout: float | None = None
    ) -> TerminalTurnResult:
        self.waited.append((turn_id, timeout))
        return TerminalTurnResult(
            status="completed",
            assistant_text="fake reply",
            errors=[],
        )

    async def stream_turn_events(self, turn_id: str):
        self.streamed.append(turn_id)
        yield 'event: zeroclaw\ndata: {"message":{"method":"message.delta","params":{"text":"hi"}}}\n\n'

    async def close(self) -> None:
        self.closed = True


def _relaunch_path(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / "threads" / session_id / "relaunch.json"


def _session_state_path(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / "threads" / session_id / "session-state.json"


@pytest.mark.asyncio
async def test_supervisor_persists_managed_workspace_launch_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    supervisor = ZeroClawSupervisor(["zeroclaw"])

    session_id = await supervisor.create_session(workspace_root, title="ZeroClaw Main")
    turn_id = await supervisor.start_turn(
        workspace_root,
        session_id,
        "hello",
        model="openrouter/gpt-5",
    )

    assert turn_id == "turn-1"
    client = _FakeZeroClawClient.instances[0]
    assert client.runtime_workspace_root == workspace_root / "workspace"
    assert client.session_state_file == _session_state_path(workspace_root, session_id)

    payload = json.loads(_relaunch_path(workspace_root, session_id).read_text())
    assert payload["session_id"] == session_id
    assert payload["title"] == "ZeroClaw Main"
    assert payload["launch_provider"] == "openrouter"
    assert payload["launch_model"] == "gpt-5"


@pytest.mark.asyncio
async def test_supervisor_rehydrates_durable_session_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    supervisor = ZeroClawSupervisor(["zeroclaw"])
    session_id = await supervisor.create_session(workspace_root, title="Resume Test")
    await supervisor.start_turn(
        workspace_root,
        session_id,
        "first turn",
        model="openrouter/gpt-5",
    )

    supervisor_after_restart = ZeroClawSupervisor(["zeroclaw"])
    listed = await supervisor_after_restart.list_sessions(workspace_root)
    resumed_id = await supervisor_after_restart.attach_session(
        workspace_root, session_id
    )
    turn_id = await supervisor_after_restart.start_turn(
        workspace_root,
        session_id,
        "second turn",
        model="openrouter/gpt-5",
    )

    assert listed == [session_id]
    assert resumed_id == session_id
    assert turn_id == "turn-1"

    resumed_client = _FakeZeroClawClient.instances[-1]
    assert resumed_client.runtime_workspace_root == workspace_root / "workspace"
    assert resumed_client.session_state_file == _session_state_path(
        workspace_root, session_id
    )
    assert resumed_client.launch_provider == "openrouter"
    assert resumed_client.launch_model == "gpt-5"
    assert resumed_client.started == [("second turn", "openrouter", "gpt-5")]


@pytest.mark.asyncio
async def test_supervisor_rejects_cross_workspace_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    other_workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-other"
    supervisor = ZeroClawSupervisor(["zeroclaw"])
    session_id = await supervisor.create_session(workspace_root)

    with pytest.raises(Exception, match="different workspace"):
        await supervisor.attach_session(other_workspace_root, session_id)
