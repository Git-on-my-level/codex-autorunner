from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from codex_autorunner.integrations.docker.profile_contracts import (
    FULL_DEV_PROFILE_CONTRACT,
)
from codex_autorunner.integrations.docker.runtime import (
    DockerRuntime,
    build_docker_container_spec,
)

pytestmark = pytest.mark.integration


def _enabled() -> bool:
    return os.environ.get("CAR_TEST_DOCKER_FULL_DEV") == "1"


def test_full_dev_profile_required_binaries_probe(tmp_path: Path) -> None:
    if not _enabled():
        pytest.skip("Set CAR_TEST_DOCKER_FULL_DEV=1 to enable full-dev image probe")

    image = str(os.environ.get("CAR_TEST_DOCKER_FULL_DEV_IMAGE", "")).strip()
    if not image:
        pytest.skip(
            "Set CAR_TEST_DOCKER_FULL_DEV_IMAGE=<image> to probe required binaries"
        )

    runtime = DockerRuntime()
    readiness = runtime.probe_readiness()
    if not readiness.binary_available:
        pytest.skip(f"Docker binary not available in PATH: {readiness.detail}")
    if not readiness.daemon_reachable:
        pytest.skip(f"Docker daemon unreachable: {readiness.detail}")

    name = f"car-full-dev-probe-{uuid.uuid4().hex[:12]}"
    spec = build_docker_container_spec(
        name=name,
        image=image,
        repo_root=tmp_path,
        workdir=str(tmp_path),
    )

    try:
        runtime.ensure_container_running(spec)
        runtime.preflight_container(
            name,
            required_binaries=FULL_DEV_PROFILE_CONTRACT.required_binaries,
            workdir=str(tmp_path),
        )
    finally:
        runtime.stop_container(name, remove=True)
