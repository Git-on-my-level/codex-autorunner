from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_autorunner.agents.opencode import supervisor as supervisor_module
from codex_autorunner.agents.opencode.supervisor import (
    OpenCodeHandle,
    OpenCodeSupervisor,
    OpenCodeSupervisorAttachAuthError,
    OpenCodeSupervisorAttachEndpointMismatchError,
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


def _expected_registry_lock_path(registry_root: Path, handle_id: str) -> Path:
    return (
        registry_root / ".codex-autorunner" / "locks" / "opencode" / f"{handle_id}.lock"
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

    written_records: list[ProcessRecord] = []
    written_paths: list[Path] = []

    def _capture_write(repo_root: Path, record: ProcessRecord, **_kwargs):
        captured["repo_root"] = repo_root
        written_records.append(record)
        written_paths.append(
            repo_root
            / ".codex-autorunner"
            / "processes"
            / "opencode"
            / f"{record.record_key()}.json"
        )
        return written_paths[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(supervisor, "_read_base_url", _fake_read_base_url)
    monkeypatch.setattr(supervisor_module, "OpenCodeClient", _FakeClient)
    monkeypatch.setattr(supervisor, "_start_stdout_drain", lambda _h: None)
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(supervisor_module, "write_process_record", _capture_write)

    await supervisor._start_process(handle)

    assert handle.started is True
    assert written_records
    record = written_records[0]
    assert isinstance(record, ProcessRecord)
    assert record.kind == "opencode"
    assert record.workspace_id == "ws-1"
    assert record.pid == 4242
    assert record.pgid == 4242
    assert record.base_url == "http://127.0.0.1:7788"
    assert record.command == ["opencode", "serve"]
    workspace_record = next(
        r for r in written_records if r.workspace_id == "ws-1" and r.pid == 4242
    )
    pid_record = next(
        r for r in written_records if r.workspace_id is None and r.pid == 4242
    )
    assert workspace_record.metadata == {"workspace_root": str(tmp_path)}
    assert pid_record.metadata["workspace_root"] == str(tmp_path)
    assert pid_record.metadata["workspace_id"] == "ws-1"
    assert written_paths[0].name == "ws-1.json"
    assert written_paths[1].name == "4242.json"


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
    refresh_calls: list[ProcessRecord] = []

    async def _fake_start_process(_handle: OpenCodeHandle) -> None:
        start_calls.append("spawned")

    async def _fake_attach(_handle: OpenCodeHandle, base_url: str) -> None:
        attach_calls.append(base_url)
        _handle.base_url = base_url
        _handle.client = object()
        _handle.started = True
        _handle.openapi_spec = {"paths": {"/global/health": {}}}

    def _capture_refresh(_handle: OpenCodeHandle, record: ProcessRecord) -> None:
        refresh_calls.append(record)

    monkeypatch.setattr(
        supervisor_module, "read_process_record", lambda *_a, **_k: registry_record
    )
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(supervisor, "_attach_to_base_url", _fake_attach)
    monkeypatch.setattr(supervisor, "_start_process", _fake_start_process)
    monkeypatch.setattr(supervisor, "_refresh_registry_ownership", _capture_refresh)

    await supervisor._ensure_started(handle)

    assert attach_calls == ["http://127.0.0.1:9001"]
    assert start_calls == []
    assert handle.started is True
    assert len(refresh_calls) == 1
    assert refresh_calls[0].workspace_id == "ws-1"


@pytest.mark.anyio
async def test_attach_to_base_url_reuses_client_health_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(
        ["opencode", "serve"], username="alice", password="s3cret", request_timeout=4.5
    )
    handle = _handle(tmp_path)
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(
            self,
            base_url: str,
            *,
            auth: tuple[str, str] | None,
            timeout: float | None,
            max_text_chars: None | int,
            logger: object,
        ) -> None:
            captured["base_url"] = base_url
            captured["auth"] = auth
            captured["timeout"] = timeout
            captured["max_text_chars"] = max_text_chars
            captured["logger"] = logger

        async def health(self) -> dict[str, object]:
            return {"version": "1.2.3"}

        async def fetch_openapi_spec(self) -> dict[str, object]:
            return {"paths": {"/global/health": {}}}

    monkeypatch.setattr(supervisor_module, "OpenCodeClient", _FakeClient)

    await supervisor._attach_to_base_url(handle, "http://127.0.0.1:9010")

    assert handle.started is True
    assert handle.base_url == "http://127.0.0.1:9010"
    assert handle.health_info == {"version": "1.2.3"}
    assert handle.version == "1.2.3"
    assert captured["base_url"] == "http://127.0.0.1:9010"
    assert captured["auth"] == ("alice", "s3cret")
    assert captured["timeout"] == 4.5


@pytest.mark.anyio
async def test_attach_to_base_url_handles_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)

    class _FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def health(self) -> dict[str, object]:
            request = httpx.Request("GET", "http://127.0.0.1:9011/health")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError(
                "unauthorized", request=request, response=response
            )

        async def fetch_openapi_spec(self) -> dict[str, object]:
            raise AssertionError("fetch_openapi_spec should not be called")

    monkeypatch.setattr(supervisor_module, "OpenCodeClient", _FakeClient)

    with pytest.raises(
        OpenCodeSupervisorAttachAuthError, match="OPENCODE_SERVER_PASSWORD"
    ):
        await supervisor._attach_to_base_url(handle, "http://127.0.0.1:9011")


@pytest.mark.anyio
async def test_ensure_started_from_registry_skips_restart_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)
    registry_record = ProcessRecord(
        kind="opencode",
        workspace_id="ws-1",
        pid=9994,
        pgid=9994,
        base_url="http://127.0.0.1:9012",
        command=["opencode", "serve"],
        owner_pid=111,
        started_at="2026-02-15T00:00:00Z",
        metadata={},
    )
    delete_calls: list[tuple[Path, str, str]] = []
    terminate_calls: list[tuple[int | None, int | None]] = []
    start_calls: list[str] = []

    async def _fake_attach(_handle: OpenCodeHandle, _base_url: str) -> None:
        raise OpenCodeSupervisorAttachAuthError("auth failed", status_code=401)

    async def _fake_start_process(_handle: OpenCodeHandle) -> None:
        start_calls.append("spawned")
        _handle.started = True

    async def _fake_terminate(record) -> bool:
        terminate_calls.append((record.pid, record.pgid))
        return True

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
    monkeypatch.setattr(supervisor, "_terminate_record_process", _fake_terminate)

    with pytest.raises(OpenCodeSupervisorAttachAuthError):
        await supervisor._ensure_started_from_registry(handle)

    assert terminate_calls == []
    assert delete_calls == []
    assert start_calls == []


def test_registry_lock_path_uses_expected_directory() -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    registry_root = Path("/tmp/registry")
    workspace_id = "ws-1"

    assert supervisor._registry_lock_path(
        registry_root, workspace_id
    ) == _expected_registry_lock_path(registry_root, workspace_id)


def test_registry_lock_path_supports_global_handle() -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"], server_scope="global")
    registry_root = Path("/tmp/registry")

    assert supervisor._registry_lock_path(
        registry_root, "__global__"
    ) == _expected_registry_lock_path(registry_root, "__global__")


@pytest.mark.anyio
async def test_ensure_started_from_registry_acquires_registry_lock_for_attach_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)
    registry_record = ProcessRecord(
        kind="opencode",
        workspace_id="ws-1",
        pid=9994,
        pgid=9994,
        base_url="http://127.0.0.1:9012",
        command=["opencode", "serve"],
        owner_pid=111,
        started_at="2026-02-15T00:00:00Z",
        metadata={},
    )

    lock_events: list[str] = []
    expected_lock_path = _expected_registry_lock_path(tmp_path, handle.workspace_id)

    def _fake_lock(path: Path):
        class _Context:
            def __enter__(self) -> None:
                lock_events.append(f"enter:{path}")

            def __exit__(self, *_args) -> None:
                lock_events.append(f"exit:{path}")

        return _Context()

    async def _fake_attach(_handle: OpenCodeHandle, base_url: str) -> None:
        assert lock_events
        assert lock_events[-1] == f"enter:{expected_lock_path}"
        _handle.base_url = base_url
        _handle.client = object()
        _handle.started = True

    monkeypatch.setattr(
        supervisor_module, "read_process_record", lambda *_a, **_k: registry_record
    )
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(supervisor, "_attach_to_base_url", _fake_attach)
    monkeypatch.setattr(supervisor_module, "file_lock", _fake_lock)

    await supervisor._ensure_started_from_registry(handle)

    assert lock_events[0] == f"enter:{expected_lock_path}"
    assert lock_events[-1] == f"exit:{expected_lock_path}"


@pytest.mark.anyio
async def test_attach_to_base_url_handles_endpoint_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)

    class _FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def health(self) -> dict[str, object]:
            request = httpx.Request("GET", "http://127.0.0.1:9013/global/health")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

        async def fetch_openapi_spec(self) -> dict[str, object]:
            raise AssertionError("fetch_openapi_spec should not be called")

    monkeypatch.setattr(supervisor_module, "OpenCodeClient", _FakeClient)

    with pytest.raises(OpenCodeSupervisorAttachEndpointMismatchError):
        await supervisor._attach_to_base_url(handle, "http://127.0.0.1:9013")


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

    pid_state = {"running": True}

    monkeypatch.setattr(
        supervisor_module, "read_process_record", lambda *_a, **_k: registry_record
    )
    monkeypatch.setattr(
        supervisor, "_pid_is_running", lambda _pid: pid_state["running"]
    )
    monkeypatch.setattr(supervisor, "_attach_to_base_url", _fake_attach)
    monkeypatch.setattr(supervisor, "_start_process", _fake_start_process)
    monkeypatch.setattr(
        supervisor_module,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )

    def _fake_killpg(_pgid: int, _sig: int) -> None:
        if _sig == 0:
            raise OSError("process group ended")
        killpg_calls.append((_pgid, _sig))
        pid_state["running"] = False

    def _fake_kill(_pid: int, _sig: int) -> None:
        kill_calls.append((_pid, _sig))
        pid_state["running"] = False

    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.killpg",
        _fake_killpg,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.process_termination.os.kill",
        _fake_kill,
    )

    await supervisor._ensure_started(handle)

    assert killpg_calls == [
        (9992, signal.SIGTERM),
        (9992, signal.SIGKILL),
    ]
    assert kill_calls == [(9992, signal.SIGTERM), (9992, signal.SIGKILL)]
    assert delete_calls == [
        (tmp_path, "opencode", "ws-1"),
        (tmp_path, "opencode", "9992"),
    ]
    assert start_calls == ["spawned"]


@pytest.mark.anyio
async def test_close_handle_skips_delete_when_process_not_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)
    assert handle.process is None
    delete_calls: list[tuple[Path, str, str]] = []

    monkeypatch.setattr(
        supervisor_module,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )

    await supervisor._close_handle(handle, reason="close_all")

    assert delete_calls == []


@pytest.mark.anyio
async def test_close_handle_deletes_registry_record_when_process_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = OpenCodeSupervisor(["opencode", "serve"])
    handle = _handle(tmp_path)

    class _FakeProcess:
        pid = 123
        returncode = None

        def terminate(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            return 0

    handle.process = _FakeProcess()
    delete_calls: list[tuple[Path, str, str]] = []

    monkeypatch.setattr(
        supervisor_module,
        "delete_process_record",
        lambda repo_root, kind, key: delete_calls.append((repo_root, kind, key))
        or True,
    )

    await supervisor._close_handle(handle, reason="close_all")

    assert sorted((call[1], call[2]) for call in delete_calls) == [
        ("opencode", "123"),
        ("opencode", "ws-1"),
    ]


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
