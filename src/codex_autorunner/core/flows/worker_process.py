from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, Optional, Tuple

_WORKER_METADATA_FILENAME = "worker.json"


@dataclass
class FlowWorkerHealth:
    status: Literal["absent", "alive", "dead", "invalid", "mismatch"]
    pid: Optional[int]
    cmdline: list[str]
    artifact_path: Path
    message: Optional[str] = None

    @property
    def is_alive(self) -> bool:
        return self.status == "alive"


def _normalized_run_id(run_id: str) -> str:
    return str(uuid.UUID(str(run_id)))


def _worker_artifacts_dir(
    repo_root: Path, run_id: str, artifacts_root: Optional[Path] = None
) -> Path:
    repo_root = repo_root.resolve()
    base_artifacts = (
        artifacts_root
        if artifacts_root is not None
        else repo_root / ".codex-autorunner" / "flows"
    )
    artifacts_dir = base_artifacts / _normalized_run_id(run_id)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return artifacts_dir


def _worker_metadata_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / _WORKER_METADATA_FILENAME


def _build_worker_cmd(entrypoint: str, run_id: str) -> list[str]:
    normalized_run_id = _normalized_run_id(run_id)
    return [
        sys.executable,
        "-m",
        entrypoint,
        "flow",
        "worker",
        "--run-id",
        normalized_run_id,
    ]


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not own it.
        return True
    except OSError:
        return False
    return True


def _read_process_cmdline(pid: int) -> list[str] | None:
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            raw = proc_path.read_bytes()
            return [part for part in raw.decode().split("\0") if part]
        except Exception:
            pass

    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
        )
        cmd = out.decode().strip()
        if cmd:
            return cmd.split()
    except Exception:
        return None
    return None


def _cmdline_matches(expected: list[str], actual: list[str]) -> bool:
    if not expected or not actual:
        return False
    if len(actual) >= len(expected) and actual[-len(expected) :] == expected:
        return True
    expected_str = " ".join(expected)
    actual_str = " ".join(actual)
    return expected_str in actual_str


def _write_worker_metadata(path: Path, pid: int, cmd: list[str]) -> None:
    data = {
        "pid": pid,
        "cmd": cmd,
        "cwd": os.getcwd(),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Also emit a plain PID file for quick inspection.
    pid_path = path.with_suffix(".pid")
    pid_path.write_text(str(pid), encoding="utf-8")


def clear_worker_metadata(artifacts_dir: Path) -> None:
    for name in (
        _WORKER_METADATA_FILENAME,
        f"{Path(_WORKER_METADATA_FILENAME).stem}.pid",
    ):
        try:
            (artifacts_dir / name).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def check_worker_health(
    repo_root: Path,
    run_id: str,
    *,
    artifacts_root: Optional[Path] = None,
    entrypoint: str = "codex_autorunner",
) -> FlowWorkerHealth:
    artifacts_dir = _worker_artifacts_dir(repo_root, run_id, artifacts_root)
    metadata_path = _worker_metadata_path(artifacts_dir)

    if not metadata_path.exists():
        return FlowWorkerHealth(
            status="absent",
            pid=None,
            cmdline=[],
            artifact_path=metadata_path,
            message="worker metadata missing",
        )

    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        pid = int(data.get("pid")) if data.get("pid") is not None else None
        cmd = data.get("cmd") or []
    except Exception:
        return FlowWorkerHealth(
            status="invalid",
            pid=None,
            cmdline=[],
            artifact_path=metadata_path,
            message="worker metadata unreadable",
        )

    if not pid or pid <= 0:
        return FlowWorkerHealth(
            status="invalid",
            pid=pid,
            cmdline=cmd if isinstance(cmd, list) else [],
            artifact_path=metadata_path,
            message="missing or invalid PID",
        )

    if not _pid_is_running(pid):
        return FlowWorkerHealth(
            status="dead",
            pid=pid,
            cmdline=cmd if isinstance(cmd, list) else [],
            artifact_path=metadata_path,
            message="worker PID not running",
        )

    expected_cmd = _build_worker_cmd(entrypoint, run_id)
    actual_cmd = _read_process_cmdline(pid)
    if actual_cmd is None:
        # Can't inspect cmdline; trust the PID check.
        return FlowWorkerHealth(
            status="alive",
            pid=pid,
            cmdline=cmd if isinstance(cmd, list) else [],
            artifact_path=metadata_path,
            message="worker running (cmdline unknown)",
        )

    if not _cmdline_matches(expected_cmd, actual_cmd):
        return FlowWorkerHealth(
            status="mismatch",
            pid=pid,
            cmdline=actual_cmd,
            artifact_path=metadata_path,
            message="worker PID command does not match expected",
        )

    return FlowWorkerHealth(
        status="alive",
        pid=pid,
        cmdline=actual_cmd,
        artifact_path=metadata_path,
        message="worker running",
    )


def spawn_flow_worker(
    repo_root: Path,
    run_id: str,
    *,
    artifacts_root: Optional[Path] = None,
    entrypoint: str = "codex_autorunner",
) -> Tuple[subprocess.Popen, IO[bytes], IO[bytes]]:
    """Spawn a detached flow worker with consistent artifacts/log layout."""

    normalized_run_id = _normalized_run_id(run_id)
    repo_root = repo_root.resolve()
    artifacts_dir = _worker_artifacts_dir(repo_root, normalized_run_id, artifacts_root)

    stdout_path = artifacts_dir / "worker.out.log"
    stderr_path = artifacts_dir / "worker.err.log"

    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")

    cmd = _build_worker_cmd(entrypoint, normalized_run_id)

    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )

    _write_worker_metadata(_worker_metadata_path(artifacts_dir), proc.pid, cmd)
    return proc, stdout_handle, stderr_handle
