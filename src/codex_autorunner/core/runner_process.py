from __future__ import annotations

import atexit
import logging
import subprocess
import sys
from pathlib import Path
from typing import Set

_process_registry: Set[subprocess.Popen] = set()
logger = logging.getLogger(__name__)


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
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _process_registry.add(proc)
    return proc


def cleanup_processes() -> None:
    for proc in list(_process_registry):
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                logger.debug("Failed to terminate runner process", exc_info=True)
            try:
                proc.wait(timeout=5)
            except Exception:
                logger.debug("Runner process wait timed out", exc_info=True)
                try:
                    proc.kill()
                except Exception:
                    logger.debug("Failed to kill runner process", exc_info=True)
    _process_registry.clear()


atexit.register(cleanup_processes)
