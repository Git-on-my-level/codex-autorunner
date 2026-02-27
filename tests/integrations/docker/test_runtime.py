from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from codex_autorunner.integrations.docker.runtime import (
    DockerRuntime,
    DockerRuntimeError,
    DockerUnavailableError,
    build_docker_container_spec,
    normalize_mounts,
    select_passthrough_env,
)


def _proc(
    args: Sequence[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=list(args), returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_is_available_false_when_binary_missing() -> None:
    def _run(*args, **kwargs):  # type: ignore[no-untyped-def]
        _ = args, kwargs
        raise FileNotFoundError("docker missing")

    runtime = DockerRuntime(run_fn=_run)
    assert runtime.is_available() is False


def test_probe_readiness_reports_daemon_unreachable() -> None:
    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        if cmd[1] == "--version":
            return _proc(cmd, stdout="Docker version 27.0.0\n")
        if cmd[1] == "info":
            return _proc(
                cmd,
                returncode=1,
                stderr="Cannot connect to the Docker daemon",
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    readiness = runtime.probe_readiness()
    assert readiness.binary_available is True
    assert readiness.daemon_reachable is False
    assert "Cannot connect to the Docker daemon" in readiness.detail


def test_probe_readiness_reports_success() -> None:
    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        if cmd[1] == "--version":
            return _proc(cmd, stdout="Docker version 27.0.0\n")
        if cmd[1] == "info":
            return _proc(cmd, stdout="27.0.0\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    readiness = runtime.probe_readiness()
    assert readiness.binary_available is True
    assert readiness.daemon_reachable is True
    assert readiness.ready is True


def test_select_passthrough_env_supports_wildcards() -> None:
    selected = select_passthrough_env(
        ["CAR_*", "PATH"],
        source_env={
            "CAR_TOKEN": "x",
            "CAR_MODE": "y",
            "PATH": "/usr/bin",
            "SECRET": "nope",
        },
    )
    assert selected == {
        "CAR_MODE": "y",
        "CAR_TOKEN": "x",
        "PATH": "/usr/bin",
    }


def test_select_passthrough_env_defaults_to_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_TEST_TOKEN", "present")
    selected = select_passthrough_env(["CAR_TEST_*"])
    assert selected == {"CAR_TEST_TOKEN": "present"}


def test_normalize_mounts_adds_repo_root_mount(tmp_path: Path) -> None:
    mounts = normalize_mounts(
        tmp_path, mounts=[{"source": "/tmp/a", "target": "/tmp/a"}]
    )
    assert mounts[0].source == str(tmp_path.resolve())
    assert mounts[0].target == str(tmp_path.resolve())
    assert any(m.source == "/tmp/a" and m.target == "/tmp/a" for m in mounts)


def test_ensure_container_running_starts_existing_stopped_container() -> None:
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        _ = kwargs
        if cmd[1] == "inspect":
            return _proc(cmd, stdout="false\n")
        if cmd[1] == "start":
            return _proc(cmd, stdout="demo\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    spec = build_docker_container_spec(
        name="demo",
        image="busybox:latest",
        repo_root=Path("/tmp/repo"),
    )
    runtime.ensure_container_running(spec)
    assert calls[0][:3] == ["docker", "inspect", "--format"]
    assert calls[1] == ["docker", "start", "demo"]


def test_ensure_container_running_runs_new_container_when_missing(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        _ = kwargs
        if cmd[1] == "inspect":
            return _proc(cmd, returncode=1, stderr="Error: No such object: demo")
        if cmd[1] == "run":
            return _proc(cmd, stdout="container-id")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    spec = build_docker_container_spec(
        name="demo",
        image="busybox:latest",
        repo_root=tmp_path,
        env_passthrough_patterns=["CAR_*"],
        explicit_env={"EXTRA": "1"},
        source_env={"CAR_TOKEN": "abc", "OTHER": "no"},
        workdir=str(tmp_path),
    )
    runtime.ensure_container_running(spec)
    run_call = calls[1]
    assert run_call[:5] == ["docker", "run", "-d", "--name", "demo"]
    assert "-v" in run_call
    assert f"{tmp_path.resolve()}:{tmp_path.resolve()}" in run_call
    assert "-w" in run_call
    assert str(tmp_path) in run_call
    assert "busybox:latest" in run_call
    assert "tail" in run_call and "/dev/null" in run_call
    assert "-e" in run_call
    assert "CAR_TOKEN=abc" in run_call
    assert "EXTRA=1" in run_call


def test_build_docker_container_spec_ignores_invalid_explicit_env(
    tmp_path: Path,
) -> None:
    spec = build_docker_container_spec(
        name="demo",
        image="busybox:latest",
        repo_root=tmp_path,
        explicit_env=["not-a-mapping"],  # type: ignore[arg-type]
    )
    assert spec.env == {}


def test_ensure_container_running_raises_on_run_failure() -> None:
    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        if cmd[1] == "inspect":
            return _proc(cmd, returncode=1, stderr="Error: No such object: demo")
        if cmd[1] == "run":
            return _proc(cmd, returncode=125, stderr="pull access denied")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    spec = build_docker_container_spec(
        name="demo",
        image="does-not-exist:latest",
        repo_root=Path("/tmp/repo"),
    )
    with pytest.raises(DockerRuntimeError, match="Failed to create container demo"):
        runtime.ensure_container_running(spec)


def test_run_exec_passes_workdir_and_env() -> None:
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        _ = kwargs
        return _proc(cmd, stdout="ok\n")

    runtime = DockerRuntime(run_fn=_run)
    result = runtime.run_exec(
        "demo",
        ["sh", "-lc", "echo ok"],
        workdir="/workspace",
        env={"CAR_TOKEN": "abc"},
    )
    assert result.stdout == "ok\n"
    cmd = calls[0]
    assert cmd[:3] == ["docker", "exec", "-w"]
    assert "/workspace" in cmd
    assert "-e" in cmd
    assert "CAR_TOKEN=abc" in cmd
    assert cmd[-3:] == ["sh", "-lc", "echo ok"]


def test_run_exec_raises_with_stderr_details() -> None:
    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        return _proc(cmd, returncode=1, stderr="boom from exec")

    runtime = DockerRuntime(run_fn=_run)
    with pytest.raises(DockerRuntimeError, match="boom from exec"):
        runtime.run_exec("demo", ["false"])


def test_stop_container_returns_false_for_missing_container() -> None:
    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        if cmd[1] == "inspect":
            return _proc(cmd, returncode=1, stderr="No such object")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    assert runtime.stop_container("missing") is False


def test_reap_container_if_expired_stops_container() -> None:
    calls: list[list[str]] = []
    started_at = "2020-01-01T00:00:00Z"

    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        _ = kwargs
        if (
            cmd[1] == "inspect"
            and cmd[2] == "--format"
            and "{{.State.StartedAt}}" in cmd[3]
        ):
            return _proc(cmd, stdout=started_at)
        if cmd[1] == "inspect":
            return _proc(cmd, stdout="true\n")
        if cmd[1] == "stop":
            return _proc(cmd, stdout="demo\n")
        if cmd[1] == "rm":
            return _proc(cmd, stdout="demo\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    reaped = runtime.reap_container_if_expired(
        "demo",
        ttl_seconds=60,
        now=dt.datetime(2020, 1, 1, 0, 2, 0, tzinfo=dt.timezone.utc),
    )
    assert reaped is True
    assert any(cmd[1] == "stop" for cmd in calls)
    assert any(cmd[1] == "rm" for cmd in calls)


def test_reap_container_if_not_expired_is_noop() -> None:
    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        _ = kwargs
        if cmd[1] == "inspect":
            return _proc(cmd, stdout="2020-01-01T00:00:00Z")
        raise AssertionError(f"Unexpected command: {cmd}")

    runtime = DockerRuntime(run_fn=_run)
    reaped = runtime.reap_container_if_expired(
        "demo",
        ttl_seconds=3600,
        now=dt.datetime(2020, 1, 1, 0, 10, 0, tzinfo=dt.timezone.utc),
    )
    assert reaped is False


def test_build_docker_container_spec_requires_name_and_image(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="container name is required"):
        build_docker_container_spec(name="", image="busybox", repo_root=tmp_path)
    with pytest.raises(ValueError, match="docker image is required"):
        build_docker_container_spec(name="demo", image="", repo_root=tmp_path)


def test_run_exec_raises_if_docker_binary_missing() -> None:
    def _run(*args, **kwargs):  # type: ignore[no-untyped-def]
        _ = args, kwargs
        raise FileNotFoundError("no docker")

    runtime = DockerRuntime(run_fn=_run)
    with pytest.raises(DockerUnavailableError):
        runtime.run_exec("demo", ["echo", "ok"])
