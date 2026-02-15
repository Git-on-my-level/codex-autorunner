from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from codex_autorunner.agents.opencode import supervisor as supervisor_module
from codex_autorunner.agents.opencode.supervisor import (
    OpenCodeHandle,
    OpenCodeSupervisor,
)
from codex_autorunner.core.managed_processes.registry import ProcessRecord


def _handle(workspace_root: Path, workspace_id: str = "ws-1") -> OpenCodeHandle:
    return OpenCodeHandle(
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        process=None,
        client=None,
        base_url=None,
        health_info=None,
        version=None,
        openapi_spec=None,
        start_lock=asyncio.Lock(),
    )


@pytest.mark.anyio
async def test_start_process_writes_registry_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)

    class _FakeProcess:
        pid = 4242
        returncode = None
        stdout = None

        def terminate(self) -> None:
            return

        def kill(self) -> None:
            return

        async def wait(self) -> int:
            return 0

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProcess()

    async def _fake_read_base_url(_process):
        return "http://127.0.0.1:7788"

    class _FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def fetch_openapi_spec(self) -> dict[str, object]:
            return {"paths": {"/global/health": {}}}

    def _capture_write(repo_root: Path, record: ProcessRecord, **_kwargs):
        captured["repo_root"] = repo_root
        captured["record"] = record
        return repo_root / ".codex-autorunner" / "processes" / "opencode" / "ws-1.json"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(supervisor, "_read_base_url", _fake_read_base_url)
    monkeypatch.setattr(supervisor_module, "OpenCodeClient", _FakeClient)
    monkeypatch.setattr(supervisor, "_start_stdout_drain", lambda _h: None)
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(supervisor_module, "write_process_record", _capture_write)

    await supervisor._start_process(handle)

    assert handle.started is True
    assert isinstance(captured.get("record"), ProcessRecord)
    record = captured["record"]
    assert isinstance(record, ProcessRecord)
    assert record.kind == "opencode"
    assert record.workspace_id == "ws-1"
    assert record.pid == 4242
    assert record.pgid == 4242
    assert record.base_url == "http://127.0.0.1:7788"
    assert record.command == ["opencode", "serve"]


@pytest.mark.anyio
async def test_ensure_started_reuses_healthy_registry_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)
    registry_record = ProcessRecord(
        kind="opencode",
        workspace_id="ws-1",
        pid=9991,
        pgid=9991,
        base_url="http://127.0.0.1:9001",
        command=["opencode", "serve"],
        owner_pid=111,
        started_at="2026-02-15T00:00:00Z",
        metadata={},
    )
    start_calls: list[str] = []
    attach_calls: list[str] = []

    async def _fake_start_process(_handle: OpenCodeHandle) -> None:
        start_calls.append("spawned")

    async def _fake_attach(_handle: OpenCodeHandle, base_url: str) -> None:
        attach_calls.append(base_url)
        _handle.base_url = base_url
        _handle.client = object()
        _handle.started = True
        _handle.openapi_spec = {"paths": {"/global/health": {}}}

    monkeypatch.setattr(
        supervisor_module, "read_process_record", lambda *_a, **_k: registry_record
    )
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(supervisor, "_attach_to_base_url", _fake_attach)
    monkeypatch.setattr(supervisor, "_start_process", _fake_start_process)

    await supervisor._ensure_started(handle)

    assert attach_calls == ["http://127.0.0.1:9001"]
    assert start_calls == []
    assert handle.started is True


@pytest.mark.anyio
async def test_ensure_started_reaps_unhealthy_registry_record_then_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)
    registry_record = ProcessRecord(
        kind="opencode",
        workspace_id="ws-1",
        pid=9992,
        pgid=9992,
        base_url="http://127.0.0.1:9002",
        command=["opencode", "serve"],
        owner_pid=111,
        started_at="2026-02-15T00:00:00Z",
        metadata={},
    )
    delete_calls: list[tuple[Path, str, str]] = []
    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    start_calls: list[str] = []

    async def _fake_attach(_handle: OpenCodeHandle, _base_url: str) -> None:
        raise supervisor_module.OpenCodeSupervisorError("health failed")

    async def _fake_start_process(_handle: OpenCodeHandle) -> None:
        start_calls.append("spawned")
        _handle.started = True

    monkeypatch.setattr(
        supervisor_module, "read_process_record", lambda *_a, **_k: registry_record
    )
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(supervisor, "_attach_to_base_url", _fake_attach)
    monkeypatch.setattr(supervisor, "_start_process", _fake_start_process)
    monkeypatch.setattr(
        supervisor_module,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        supervisor_module.os, "kill", lambda pid, sig: kill_calls.append((pid, sig))
    )

    await supervisor._ensure_started(handle)

    assert killpg_calls == [(9992, signal.SIGTERM)]
    assert kill_calls == [(9992, signal.SIGTERM)]
    assert delete_calls and delete_calls[0][1:] == ("opencode", "ws-1")
    assert start_calls == ["spawned"]


@pytest.mark.anyio
async def test_close_handle_deletes_registry_record_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)
    delete_calls: list[tuple[Path, str, str]] = []

    monkeypatch.setattr(
        supervisor_module,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )

    await supervisor._close_handle(handle, reason="close_all")

    assert delete_calls == [(tmp_path, "opencode", "ws-1")]


@pytest.mark.anyio
async def test_global_scope_reuses_single_handle_across_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"], server_scope="global")
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    starts: list[str] = []
    client = object()

    async def _fake_start_process(handle: OpenCodeHandle) -> None:
        starts.append(handle.workspace_id)
        handle.client = client
        handle.started = True

    async def _fake_registry_reuse(_handle: OpenCodeHandle) -> bool:
        return False

    monkeypatch.setattr(supervisor, "_start_process", _fake_start_process)
    monkeypatch.setattr(
        supervisor, "_ensure_started_from_registry", _fake_registry_reuse
    )

    client_a = await supervisor.get_client(workspace_a)
    client_b = await supervisor.get_client(workspace_b)

    assert client_a is client_b is client
    assert starts == ["__global__"]
    assert list(supervisor._handles.keys()) == ["__global__"]


@pytest.mark.anyio
async def test_global_scope_close_calls_dispose_before_client_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"], server_scope="global")
    order: list[str] = []

    class _Client:
        async def dispose_instances(self) -> None:
            order.append("dispose")

        async def close(self) -> None:
            order.append("close")

    handle = OpenCodeHandle(
        workspace_id="__global__",
        workspace_root=tmp_path,
        process=None,
        client=_Client(),
        base_url="http://127.0.0.1:8000",
        health_info=None,
        version=None,
        openapi_spec=None,
        start_lock=asyncio.Lock(),
        started=True,
    )
    supervisor._handles["__global__"] = handle

    monkeypatch.setattr(
        supervisor_module, "delete_process_record", lambda *_a, **_k: True
    )

    await supervisor.close_all()

    assert order == ["dispose", "close"]
