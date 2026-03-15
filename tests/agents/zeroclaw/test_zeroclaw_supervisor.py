from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.agents.types import TerminalTurnResult
from codex_autorunner.agents.zeroclaw.supervisor import ZeroClawSupervisor
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.destinations import DockerDestination


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
        self.turn_outputs: dict[str, str] = {}
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
        turn_id = f"turn-{len(self.started)}"
        self.turn_outputs[turn_id] = self._execute_prompt(prompt)
        return turn_id

    async def wait_for_turn(
        self, turn_id: str, *, timeout: float | None = None
    ) -> TerminalTurnResult:
        self.waited.append((turn_id, timeout))
        return TerminalTurnResult(
            status="completed",
            assistant_text=self.turn_outputs.get(turn_id, "fake reply"),
            errors=[],
        )

    async def stream_turn_events(self, turn_id: str):
        self.streamed.append(turn_id)
        yield 'event: zeroclaw\ndata: {"message":{"method":"message.delta","params":{"text":"hi"}}}\n\n'

    async def close(self) -> None:
        self.closed = True

    def _execute_prompt(self, prompt: str) -> str:
        self.runtime_workspace_root.mkdir(parents=True, exist_ok=True)
        self.session_state_file.parent.mkdir(parents=True, exist_ok=True)

        if prompt.startswith("remember:"):
            value = prompt.split(":", 1)[1]
            (self.runtime_workspace_root / "shared-memory.txt").write_text(
                value,
                encoding="utf-8",
            )
            return f"remembered:{value}"
        if prompt == "recall":
            shared = self.runtime_workspace_root / "shared-memory.txt"
            if not shared.exists():
                return "missing"
            return shared.read_text(encoding="utf-8")
        if prompt.startswith("session-note:"):
            value = prompt.split(":", 1)[1]
            self.session_state_file.write_text(value, encoding="utf-8")
            return f"session:{value}"
        if prompt == "session-state":
            if not self.session_state_file.exists():
                return "missing"
            return self.session_state_file.read_text(encoding="utf-8")
        return "fake reply"


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
async def test_supervisor_multiple_sessions_share_workspace_memory_but_keep_session_state_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    supervisor = ZeroClawSupervisor(["zeroclaw"])

    session_a = await supervisor.create_session(workspace_root, title="Thread A")
    session_b = await supervisor.create_session(workspace_root, title="Thread B")

    remember_turn = await supervisor.start_turn(
        workspace_root,
        session_a,
        "remember:shared-across-threads",
    )
    remember_result = await supervisor.wait_for_turn(
        workspace_root, session_a, remember_turn
    )
    assert remember_result.assistant_text == "remembered:shared-across-threads"

    session_a_note_turn = await supervisor.start_turn(
        workspace_root,
        session_a,
        "session-note:alpha-transcript",
    )
    session_b_note_turn = await supervisor.start_turn(
        workspace_root,
        session_b,
        "session-note:beta-transcript",
    )
    session_a_note = await supervisor.wait_for_turn(
        workspace_root, session_a, session_a_note_turn
    )
    session_b_note = await supervisor.wait_for_turn(
        workspace_root, session_b, session_b_note_turn
    )
    assert session_a_note.assistant_text == "session:alpha-transcript"
    assert session_b_note.assistant_text == "session:beta-transcript"

    supervisor_after_restart = ZeroClawSupervisor(["zeroclaw"])
    listed = await supervisor_after_restart.list_sessions(workspace_root)
    assert listed == [session_a, session_b]

    resumed_a = await supervisor_after_restart.attach_session(workspace_root, session_a)
    resumed_b = await supervisor_after_restart.attach_session(workspace_root, session_b)
    assert resumed_a == session_a
    assert resumed_b == session_b

    recall_turn = await supervisor_after_restart.start_turn(
        workspace_root,
        session_b,
        "recall",
    )
    recall_result = await supervisor_after_restart.wait_for_turn(
        workspace_root,
        session_b,
        recall_turn,
    )
    session_a_state_turn = await supervisor_after_restart.start_turn(
        workspace_root,
        session_a,
        "session-state",
    )
    session_b_state_turn = await supervisor_after_restart.start_turn(
        workspace_root,
        session_b,
        "session-state",
    )
    session_a_state = await supervisor_after_restart.wait_for_turn(
        workspace_root,
        session_a,
        session_a_state_turn,
    )
    session_b_state = await supervisor_after_restart.wait_for_turn(
        workspace_root,
        session_b,
        session_b_state_turn,
    )

    assert recall_result.assistant_text == "shared-across-threads"
    assert session_a_state.assistant_text == "alpha-transcript"
    assert session_b_state.assistant_text == "beta-transcript"

    first_client, second_client = _FakeZeroClawClient.instances[:2]
    assert first_client.runtime_workspace_root == workspace_root / "workspace"
    assert second_client.runtime_workspace_root == workspace_root / "workspace"
    assert first_client.session_state_file != second_client.session_state_file
    assert (
        first_client.session_state_file.read_text(encoding="utf-8")
        == "alpha-transcript"
    )
    assert (
        second_client.session_state_file.read_text(encoding="utf-8")
        == "beta-transcript"
    )


@pytest.mark.asyncio
async def test_supervisor_keeps_workspace_memory_isolated_across_workspaces_with_one_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    workspace_a = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    workspace_b = tmp_path / "runtimes" / "zeroclaw" / "zc-other"
    supervisor = ZeroClawSupervisor(["zeroclaw"])

    session_a = await supervisor.create_session(workspace_a, title="Workspace A")
    session_b = await supervisor.create_session(workspace_b, title="Workspace B")

    remember_a_turn = await supervisor.start_turn(
        workspace_a,
        session_a,
        "remember:workspace-a",
    )
    remember_b_turn = await supervisor.start_turn(
        workspace_b,
        session_b,
        "remember:workspace-b",
    )
    await supervisor.wait_for_turn(workspace_a, session_a, remember_a_turn)
    await supervisor.wait_for_turn(workspace_b, session_b, remember_b_turn)

    recall_a_turn = await supervisor.start_turn(workspace_a, session_a, "recall")
    recall_b_turn = await supervisor.start_turn(workspace_b, session_b, "recall")
    recall_a = await supervisor.wait_for_turn(workspace_a, session_a, recall_a_turn)
    recall_b = await supervisor.wait_for_turn(workspace_b, session_b, recall_b_turn)

    assert recall_a.assistant_text == "workspace-a"
    assert recall_b.assistant_text == "workspace-b"
    assert (workspace_a / "workspace" / "shared-memory.txt").read_text(
        encoding="utf-8"
    ) == "workspace-a"
    assert (workspace_b / "workspace" / "shared-memory.txt").read_text(
        encoding="utf-8"
    ) == "workspace-b"

    assert len(_FakeZeroClawClient.instances) == 2
    assert {tuple(client.command) for client in _FakeZeroClawClient.instances} == {
        ("zeroclaw",)
    }
    assert {
        client.runtime_workspace_root for client in _FakeZeroClawClient.instances
    } == {
        workspace_a / "workspace",
        workspace_b / "workspace",
    }


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


@pytest.mark.asyncio
async def test_supervisor_wraps_workspace_launch_for_docker_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    captured: dict[str, object] = {}

    def _fake_wrap_command_for_destination(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return type(
            "Wrapped", (), {"command": ["docker", "exec", "zc-main", "zeroclaw"]}
        )()

    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor._wrap_command_for_destination",
        _fake_wrap_command_for_destination,
    )

    workspace_root = tmp_path / "runtimes" / "zeroclaw" / "zc-main"
    supervisor = ZeroClawSupervisor(
        ["zeroclaw"],
        destination_resolver=lambda _root: DockerDestination(
            image="ghcr.io/acme/zeroclaw:latest"
        ),
    )

    session_id = await supervisor.create_session(workspace_root)
    await supervisor.start_turn(workspace_root, session_id, "hello")

    runtime_workspace_root = workspace_root / "workspace"
    assert captured["repo_root"] == workspace_root
    assert captured["command_workdir"] == runtime_workspace_root
    assert captured["extra_env"] == {"ZEROCLAW_WORKSPACE": str(runtime_workspace_root)}

    client = _FakeZeroClawClient.instances[0]
    assert client.command[:4] == ["docker", "exec", "zc-main", "zeroclaw"]


@pytest.mark.asyncio
async def test_build_supervisor_from_hub_config_uses_agent_workspace_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeZeroClawClient.instances.clear()
    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor.ZeroClawClient",
        _FakeZeroClawClient,
    )

    captured: dict[str, object] = {}

    def _fake_wrap_command_for_destination(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return type(
            "Wrapped", (), {"command": ["docker", "exec", "zc-main", "zeroclaw"]}
        )()

    monkeypatch.setattr(
        "codex_autorunner.agents.zeroclaw.supervisor._wrap_command_for_destination",
        _fake_wrap_command_for_destination,
    )

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: zc-main",
                "    runtime: zeroclaw",
                "    path: .codex-autorunner/runtimes/zeroclaw/zc-main",
                "    enabled: true",
                "    destination:",
                "      kind: docker",
                "      image: ghcr.io/acme/zeroclaw:latest",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    hub_config = load_hub_config(hub_root)

    from codex_autorunner.agents.zeroclaw.supervisor import (
        build_zeroclaw_supervisor_from_config,
    )

    supervisor = build_zeroclaw_supervisor_from_config(hub_config)
    assert supervisor is not None

    workspace_root = (
        hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    session_id = await supervisor.create_session(workspace_root)
    await supervisor.start_turn(workspace_root, session_id, "hello")

    assert isinstance(captured["destination"], DockerDestination)
