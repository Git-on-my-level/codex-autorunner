from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.core.managed_processes.registry import ProcessRecord
from codex_autorunner.integrations.app_server import client as app_server_client
from codex_autorunner.integrations.app_server.client import CodexAppServerClient
from codex_autorunner.integrations.app_server.supervisor import (
    WorkspaceAppServerSupervisor,
)


@pytest.mark.anyio
async def test_app_server_spawn_registers_process_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeProcess:
        pid = 32101
        returncode = None
        stdin = object()
        stdout = object()
        stderr = object()

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            return

        def kill(self) -> None:
            return

    captured: dict[str, Any] = {}

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProcess()

    def _capture_write(repo_root: Path, record: ProcessRecord, **_kwargs):
        captured["repo_root"] = repo_root
        captured["record"] = record
        return (
            repo_root
            / ".codex-autorunner"
            / "processes"
            / "codex_app_server"
            / "ws-1.json"
        )

    client = CodexAppServerClient(
        ["python", "-m", "codex_autorunner"],
        cwd=tmp_path,
        workspace_id="ws-1",
    )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(app_server_client.os, "getpgid", lambda _pid: 32101)
    monkeypatch.setattr(app_server_client, "write_process_record", _capture_write)
    monkeypatch.setattr(client, "_read_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(client, "_drain_stderr", lambda: asyncio.sleep(0))

    await client._spawn_process()

    assert isinstance(captured.get("record"), ProcessRecord)
    record = captured["record"]
    assert isinstance(record, ProcessRecord)
    assert record.kind == "codex_app_server"
    assert record.workspace_id == "ws-1"
    assert record.pid == 32101
    assert record.pgid == 32101
    assert record.owner_pid is not None
    assert record.command == ["python", "-m", "codex_autorunner"]

    await client._terminate_process()


@pytest.mark.anyio
async def test_app_server_terminate_unregisters_and_prefers_group_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeProcess:
        pid = 32102
        returncode = None
        stdin = object()
        stdout = object()
        stderr = object()

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            return

        def kill(self) -> None:
            return

    delete_calls: list[tuple[Path, str, str]] = []
    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []

    client = CodexAppServerClient(
        ["python", "-m", "codex_autorunner"],
        cwd=tmp_path,
        workspace_id="ws-2",
    )
    client._process = _FakeProcess()
    client._process_registry_key = "ws-2"
    client._reader_task = asyncio.create_task(asyncio.sleep(0))
    client._stderr_task = asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(
        app_server_client,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )
    monkeypatch.setattr(
        app_server_client.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        app_server_client.os, "kill", lambda pid, sig: kill_calls.append((pid, sig))
    )

    await client._terminate_process()

    assert delete_calls == [(tmp_path, "codex_app_server", "ws-2")]
    assert killpg_calls == [(32102, signal.SIGTERM)]
    assert kill_calls == [(32102, signal.SIGTERM)]


@pytest.mark.anyio
async def test_app_server_supervisor_passes_workspace_id_to_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["workspace_id"] = kwargs.get("workspace_id")

        async def start(self) -> None:
            return

        async def close(self) -> None:
            return

    monkeypatch.setattr(
        "codex_autorunner.integrations.app_server.supervisor.CodexAppServerClient",
        _FakeClient,
    )

    def env_builder(
        _workspace_root: Path, _workspace_id: str, _state_dir: Path
    ) -> dict[str, str]:
        return {}

    supervisor = WorkspaceAppServerSupervisor(
        ["python", "-m", "codex_autorunner"],
        state_root=tmp_path / "state",
        env_builder=env_builder,
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    await supervisor.get_client(workspace)

    assert isinstance(captured.get("workspace_id"), str)
