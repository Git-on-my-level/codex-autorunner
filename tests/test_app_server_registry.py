from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.adapters.app_server import client as app_server_client
from codex_autorunner.adapters.app_server.client import CodexAppServerClient
from codex_autorunner.adapters.app_server.supervisor import (
    WorkspaceAppServerSupervisor,
)
from codex_autorunner.core.managed_processes.registry import ProcessRecord


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
    assert record.handle_id == "ws-1"
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
async def test_thread_archive_unregisters_thread_runtime_callbacks(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        ["python", "-m", "codex_autorunner"],
        cwd=tmp_path,
        workspace_id="ws-archive",
    )
    observed: dict[str, Any] = {}

    async def _fake_request(method: str, params: dict[str, Any]) -> dict[str, str]:
        observed["method"] = method
        observed["params"] = params
        return {"status": "ok"}

    async def _approval(_request: dict[str, Any]) -> str:
        return "cancel"

    async def _question(_request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def _notification(_message: dict[str, Any]) -> None:
        return None

    client.request = _fake_request  # type: ignore[method-assign]
    client.register_runtime_callbacks(
        thread_id="thread-archive",
        approval_handler=_approval,
        question_handler=_question,
        notification_handler=_notification,
    )

    result = await client.thread_archive("thread-archive")

    assert result == {"status": "ok"}
    assert observed == {
        "method": "thread/archive",
        "params": {"threadId": "thread-archive"},
    }
    assert "thread-archive" not in client._thread_approval_handlers
    assert "thread-archive" not in client._thread_user_input_handlers
    assert "thread-archive" not in client._thread_notification_handlers


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
        "codex_autorunner.adapters.app_server.supervisor.CodexAppServerClient",
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


@pytest.mark.anyio
async def test_force_kill_process_escalates_to_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeProcess:
        pid = 32103

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        app_server_client.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        app_server_client.os,
        "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    client = CodexAppServerClient(["python", "-m", "codex_autorunner"])
    await client._force_kill_process(_FakeProcess())

    assert killpg_calls == [(32103, signal.SIGKILL)]
    assert kill_calls == [(32103, signal.SIGKILL)]


@pytest.mark.anyio
async def test_startup_timeout_terminates_process_and_unregisters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeProcess:
        pid = 32104
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
    client = CodexAppServerClient(
        ["python", "-m", "codex_autorunner"],
        cwd=tmp_path,
        workspace_id="ws-timeout",
        startup_timeout_seconds=0.01,
        terminate_grace_seconds=0,
        terminate_kill_seconds=0,
    )

    async def _fake_spawn() -> None:
        client._process = _FakeProcess()
        client._process_registry_key = "ws-timeout"

    async def _hang_initialize() -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(client, "_spawn_process", _fake_spawn)
    monkeypatch.setattr(client, "_initialize_handshake", _hang_initialize)
    monkeypatch.setattr(
        app_server_client,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )
    monkeypatch.setattr(app_server_client.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(app_server_client.os, "kill", lambda *_args: None)

    with pytest.raises(app_server_client.CodexAppServerDisconnected):
        await client.start()

    assert delete_calls == [(tmp_path, "codex_app_server", "ws-timeout")]
    assert client._process is None


@pytest.mark.anyio
async def test_thread_scoped_approval_and_notification_routing() -> None:
    client = CodexAppServerClient(["python", "-m", "codex_autorunner"])
    approvals: list[str] = []
    notifications: list[str] = []
    sent: list[dict[str, Any]] = []

    async def approval_one(_request: dict[str, Any]) -> str:
        approvals.append("one")
        return "accept"

    async def approval_two(_request: dict[str, Any]) -> str:
        approvals.append("two")
        return "cancel"

    async def notify_one(_request: dict[str, Any]) -> None:
        notifications.append("one")

    async def notify_two(_request: dict[str, Any]) -> None:
        notifications.append("two")

    async def capture_send(message: dict[str, Any]) -> None:
        sent.append(message)

    client.register_runtime_callbacks(
        thread_id="thread-one",
        approval_handler=approval_one,
        notification_handler=notify_one,
    )
    client.register_runtime_callbacks(
        thread_id="thread-two",
        approval_handler=approval_two,
        notification_handler=notify_two,
    )
    client._send_message = capture_send  # type: ignore[method-assign]

    await client._handle_server_request(
        {
            "jsonrpc": "2.0",
            "id": "approval-2",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-two",
                "turnId": "turn-two",
                "itemId": "item-two",
            },
        }
    )
    await client._handle_notification(
        {
            "jsonrpc": "2.0",
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-one",
                "turnId": "turn-one",
                "itemId": "item-one",
                "delta": "hello",
            },
        }
    )

    assert approvals == ["two"]
    assert sent[-1]["result"] == {"decision": "cancel"}
    assert notifications == ["one"]
