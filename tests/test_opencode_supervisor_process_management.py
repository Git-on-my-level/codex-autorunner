from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from queue import Empty
from typing import Any, Mapping, Optional

import pytest

from codex_autorunner.agents.opencode.supervisor import (
    OpenCodeSupervisor,
    OpenCodeSupervisorAttachAuthError,
)
from codex_autorunner.core.managed_processes.registry import read_process_record
from codex_autorunner.workspace import canonical_workspace_root, workspace_id_for_path


def _fake_server_script() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "fake_opencode_server.py"


def _fake_server_command() -> list[str]:
    return [sys.executable, str(_fake_server_script())]


def _ensure_registry_root(workspace: Path) -> None:
    (workspace / ".codex-autorunner" / "processes" / "opencode").mkdir(
        parents=True, exist_ok=True
    )


def _workspace_id(workspace_root: Path) -> str:
    return workspace_id_for_path(canonical_workspace_root(workspace_root))


def _read_file_int(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for file {path}")
        time.sleep(0.05)


def _wait_for_marker(path: Path, minimum: int, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            count = len(
                [line for line in path.read_text(encoding="utf-8").splitlines() if line]
            )
            if count >= minimum:
                return count
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.05)


def _assert_process_gone(pid: int) -> None:
    for _ in range(120):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} still running after termination")


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _build_env(overrides: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if overrides:
        env.update(overrides)
    return env


def _cross_process_worker(
    workspace: str,
    queue: mp.Queue[Any],
    ready_barrier: Any,
    command: list[str],
    env: Mapping[str, str],
) -> None:
    async def _run() -> None:
        supervisor = OpenCodeSupervisor(
            command,
            username="opencode",
            password="expected",
            base_env=dict(env),
        )
        try:
            workspace_path = Path(workspace)
            client = await supervisor.get_client(workspace_path)
            handle = supervisor._handles[_workspace_id(workspace_path)]
            queue.put(
                {
                    "workspace_id": handle.workspace_id,
                    "base_url": handle.base_url,
                    "pid": handle.process.pid if handle.process is not None else None,
                }
            )
            ready_barrier.wait()
            await client.close()
        finally:
            await supervisor.close_all()

    try:
        asyncio.run(_run())
    except Exception as exc:
        queue.put({"error": repr(exc)})


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process groups")
@pytest.mark.anyio
async def test_close_all_kills_process_group_and_child(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_pid_path = tmp_path / "child_pid.txt"
    marker_path = tmp_path / "starts.txt"
    _ensure_registry_root(workspace)

    command = _fake_server_command()
    env = _build_env(
        {
            "OPENCODE_CHILD_PID_FILE": str(child_pid_path),
            "OPENCODE_START_MARKER_PATH": str(marker_path),
        }
    )
    supervisor = OpenCodeSupervisor(
        command,
        username="opencode",
        password="expected",
        base_env=env,
    )

    client = await supervisor.get_client(workspace)
    handle = supervisor._handles[_workspace_id(workspace)]
    try:
        if handle.process is None or handle.process.pid is None:
            raise AssertionError("process was not started")
        parent_pid = handle.process.pid
        child_pid = _read_file_int(child_pid_path)
        await client.close()
        await supervisor.close_all()
        _assert_process_gone(parent_pid)
        _assert_process_gone(child_pid)
    finally:
        await supervisor.close_all()

    assert _wait_for_marker(marker_path, minimum=1) == 1
    assert read_process_record(workspace, "opencode", _workspace_id(workspace)) is None
    assert read_process_record(workspace, "opencode", str(parent_pid)) is None


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process groups")
@pytest.mark.anyio
async def test_registry_reuse_with_basic_auth_succeeds(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker_path = tmp_path / "starts.txt"
    child_pid_path = tmp_path / "child_pid.txt"
    _ensure_registry_root(workspace)

    command = _fake_server_command()
    env = _build_env(
        {
            "OPENCODE_CHILD_PID_FILE": str(child_pid_path),
            "OPENCODE_START_MARKER_PATH": str(marker_path),
            "OPENCODE_SERVER_PASSWORD": "expected",
            "OPENCODE_SERVER_USERNAME": "opencode",
        }
    )

    first = OpenCodeSupervisor(
        command,
        username="opencode",
        password="expected",
        base_env=env,
    )
    second = OpenCodeSupervisor(
        command,
        username="opencode",
        password="expected",
        base_env=env,
    )
    try:
        first_client = await first.get_client(workspace)
        await first_client.close()
        start_count = _wait_for_marker(marker_path, minimum=1)
        first_handle = first._handles[_workspace_id(workspace)]
        if first_handle.process is None or first_handle.process.pid is None:
            raise AssertionError("first supervisor did not start server")
        first_pid = first_handle.process.pid

        second_client = await second.get_client(workspace)
        await second_client.close()
        second_handle = second._handles[_workspace_id(workspace)]

        assert _wait_for_marker(marker_path, minimum=1) == start_count
        assert second_handle.process is None
        assert second_handle.base_url == first_handle.base_url

        first_record = read_process_record(
            workspace, "opencode", _workspace_id(workspace)
        )
        if first_record is None or first_record.pid is None:
            raise AssertionError("workspace record missing after reuse")
        assert first_record.pid == first_pid
    finally:
        await first.close_all()
        await second.close_all()

    assert read_process_record(workspace, "opencode", _workspace_id(workspace)) is None
    assert read_process_record(workspace, "opencode", str(first_pid)) is None


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process groups")
@pytest.mark.anyio
async def test_auth_failure_does_not_kill_or_delete_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker_path = tmp_path / "starts.txt"
    child_pid_path = tmp_path / "child_pid.txt"
    _ensure_registry_root(workspace)

    command = _fake_server_command()
    env = _build_env(
        {
            "OPENCODE_CHILD_PID_FILE": str(child_pid_path),
            "OPENCODE_START_MARKER_PATH": str(marker_path),
            "OPENCODE_SERVER_PASSWORD": "expected",
            "OPENCODE_SERVER_USERNAME": "opencode",
        }
    )

    first = OpenCodeSupervisor(
        command,
        username="opencode",
        password="expected",
        base_env=env,
    )
    second = OpenCodeSupervisor(
        command,
        username="opencode",
        password="wrong",
        base_env=env,
    )
    try:
        first_client = await first.get_client(workspace)
        await first_client.close()
        _wait_for_marker(marker_path, minimum=1)
        ws_id = _workspace_id(workspace)
        first_record = read_process_record(workspace, "opencode", ws_id)
        if first_record is None or first_record.pid is None:
            raise AssertionError("expected record before auth failure")
        pid = first_record.pid

        with pytest.raises(
            OpenCodeSupervisorAttachAuthError,
            match="OPENCODE_SERVER_PASSWORD",
        ):
            await second.get_client(workspace)

        second_record = read_process_record(workspace, "opencode", ws_id)
        pid_record = read_process_record(workspace, "opencode", str(pid))
        assert second_record is not None
        assert pid_record is not None
        assert _pid_is_running(pid)
    finally:
        await first.close_all()
        await second.close_all()

    assert _wait_for_marker(marker_path, minimum=1) == 1
    assert read_process_record(workspace, "opencode", _workspace_id(workspace)) is None


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process groups")
def test_cross_process_lock_prevents_duplicate_spawn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker_path = tmp_path / "starts.txt"
    child_pid_path = tmp_path / "child_pid.txt"
    _ensure_registry_root(workspace)
    command = _fake_server_command()
    env = _build_env(
        {
            "OPENCODE_CHILD_PID_FILE": str(child_pid_path),
            "OPENCODE_START_MARKER_PATH": str(marker_path),
            "OPENCODE_SERVER_PASSWORD": "expected",
            "OPENCODE_SERVER_USERNAME": "opencode",
        }
    )

    context = mp.get_context()
    sync_barrier = context.Barrier(2)
    queue: mp.Queue[Any] = context.Queue()
    p1 = context.Process(
        target=_cross_process_worker,
        args=(str(workspace), queue, sync_barrier, command, env),
    )
    p2 = context.Process(
        target=_cross_process_worker,
        args=(str(workspace), queue, sync_barrier, command, env),
    )
    p1.start()
    p2.start()

    results: list[dict[str, Any]] = []
    for _ in range(2):
        try:
            item = queue.get(timeout=30)
        except Empty as exc:
            p1.terminate()
            p2.terminate()
            p1.join(timeout=1)
            p2.join(timeout=1)
            raise AssertionError("worker did not report result in time") from exc
        results.append(item)

    p1.join(timeout=30)
    p2.join(timeout=30)
    if p1.exitcode != 0:
        raise AssertionError(f"first worker exited with code {p1.exitcode}")
    if p2.exitcode != 0:
        raise AssertionError(f"second worker exited with code {p2.exitcode}")

    errors = [entry for entry in results if "error" in entry]
    if errors:
        raise AssertionError(f"worker errors: {errors}")

    assert _wait_for_marker(marker_path, minimum=1) == 1
    workspace_id = _workspace_id(workspace)
    base_urls = {entry["base_url"] for entry in results if "base_url" in entry}
    assert len(base_urls) == 1
    assert any(entry.get("pid") is not None for entry in results if "pid" in entry)
    assert read_process_record(workspace, "opencode", workspace_id) is None
