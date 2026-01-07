from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def build_runner_cmd(repo_root: Path, *, action: str, once: bool = False) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "codex_autorunner.cli",
        action,
        "--repo",
        str(repo_root),
    ]
    if action == "resume" and once:
        cmd.append("--once")
    return cmd


def spawn_detached(cmd: list[str], *, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
