from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from codex_autorunner.core.destinations import DockerDestination
from codex_autorunner.integrations.agents.destination_wrapping import (
    wrap_command_for_destination,
)


class _FakeDockerRuntime:
    def __init__(self) -> None:
        self.ensure_calls: list[Any] = []
        self.preflight_calls: list[dict[str, Any]] = []
        self.exec_calls: list[dict[str, Any]] = []

    def ensure_container_running(self, spec: Any) -> None:
        self.ensure_calls.append(spec)

    def preflight_container(
        self,
        container_name: str,
        *,
        required_binaries: Sequence[str] = (),
        required_readable_files: Sequence[str] = (),
        workdir: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.preflight_calls.append(
            {
                "container_name": container_name,
                "required_binaries": tuple(required_binaries),
                "required_readable_files": tuple(required_readable_files),
                "workdir": workdir,
                "timeout_seconds": timeout_seconds,
            }
        )

    def build_exec_command(
        self,
        container_name: str,
        command: Sequence[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        self.exec_calls.append(
            {
                "container_name": container_name,
                "command": [str(part) for part in command],
                "workdir": workdir,
                "env": dict(env or {}),
            }
        )
        return ["docker", "exec", container_name, *[str(part) for part in command]]


def test_wrap_command_for_destination_skips_preflight_without_profile(
    tmp_path: Path,
) -> None:
    runtime = _FakeDockerRuntime()
    destination = DockerDestination(image="busybox:latest")

    wrapped = wrap_command_for_destination(
        command=["codex", "app-server"],
        destination=destination,
        repo_root=tmp_path,
        docker_runtime=runtime,  # type: ignore[arg-type]
    )

    assert wrapped.command[:3] == [
        "docker",
        "exec",
        runtime.exec_calls[0]["container_name"],
    ]
    assert len(runtime.ensure_calls) == 1
    assert runtime.preflight_calls == []


def test_wrap_command_for_destination_runs_profile_preflight_for_full_dev(
    tmp_path: Path,
) -> None:
    runtime = _FakeDockerRuntime()
    destination = DockerDestination(image="busybox:latest", profile="full-dev")

    wrap_command_for_destination(
        command=["codex", "app-server"],
        destination=destination,
        repo_root=tmp_path,
        docker_runtime=runtime,  # type: ignore[arg-type]
    )

    assert len(runtime.preflight_calls) == 1
    preflight = runtime.preflight_calls[0]
    assert "codex" in preflight["required_binaries"]
    assert any(
        str(path).endswith("/.codex/auth.json")
        for path in preflight["required_readable_files"]
    )
