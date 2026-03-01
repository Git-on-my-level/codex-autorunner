from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from codex_autorunner.integrations.docker.runtime import (
    DockerRuntime,
    build_docker_container_spec,
)

pytestmark = pytest.mark.integration


def _enabled() -> bool:
    return os.environ.get("CAR_TEST_DOCKER") == "1"


def test_docker_exec_smoke(tmp_path: Path) -> None:
    if not _enabled():
        pytest.skip("Set CAR_TEST_DOCKER=1 to enable docker smoke test")

    runtime = DockerRuntime()
    readiness = runtime.probe_readiness()
    if not readiness.binary_available:
        pytest.skip(f"Docker binary not available in PATH: {readiness.detail}")
    if not readiness.daemon_reachable:
        pytest.skip(f"Docker daemon unreachable: {readiness.detail}")

    image = os.environ.get("CAR_TEST_DOCKER_IMAGE", "busybox:latest")
    name = f"car-test-{uuid.uuid4().hex[:12]}"
    spec = build_docker_container_spec(
        name=name,
        image=image,
        repo_root=tmp_path,
        workdir=str(tmp_path),
    )

    try:
        runtime.ensure_container_running(spec)
        result = runtime.run_exec(
            name,
            ["sh", "-lc", "echo docker-ok"],
            workdir=str(tmp_path),
        )
        assert result.stdout.strip() == "docker-ok"
    finally:
        runtime.stop_container(name, remove=True)
