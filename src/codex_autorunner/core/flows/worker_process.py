from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path
from typing import IO, Optional, Tuple


def spawn_flow_worker(
    repo_root: Path,
    run_id: str,
    *,
    artifacts_root: Optional[Path] = None,
    entrypoint: str = "codex_autorunner",
) -> Tuple[subprocess.Popen, IO[bytes], IO[bytes]]:
    """Spawn a detached flow worker with consistent artifacts/log layout."""

    normalized_run_id = str(uuid.UUID(str(run_id)))
    repo_root = repo_root.resolve()
    base_artifacts = (
        artifacts_root
        if artifacts_root is not None
        else repo_root / ".codex-autorunner" / "flows"
    )
    artifacts_dir = base_artifacts / normalized_run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = artifacts_dir / "worker.out.log"
    stderr_path = artifacts_dir / "worker.err.log"

    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")

    cmd = [
        sys.executable,
        "-m",
        entrypoint,
        "flow",
        "worker",
        "--run-id",
        normalized_run_id,
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )
    return proc, stdout_handle, stderr_handle
