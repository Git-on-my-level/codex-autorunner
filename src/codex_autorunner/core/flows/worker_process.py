from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Literal, Optional, Tuple

_WORKER_METADATA_FILENAME = "worker.json"
_WORKER_EXIT_FILENAME = "worker.exit.json"
_WORKER_CRASH_FILENAME = "crash.json"
_MAX_TAIL_BYTES = 32_768


@dataclass
class FlowWorkerHealth:
    status: Literal["absent", "alive", "dead", "invalid", "mismatch"]
    pid: Optional[int]
    cmdline: list[str]
    artifact_path: Path
    message: Optional[str] = None
    exit_code: Optional[int] = None
    stderr_tail: Optional[str] = None
    crash_path: Optional[Path] = None
    crash_info: Optional[dict[str, Any]] = None

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


def _worker_exit_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / _WORKER_EXIT_FILENAME


def _worker_crash_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / _WORKER_CRASH_FILENAME


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _signal_from_returncode(returncode: Optional[int]) -> Optional[str]:
    if not isinstance(returncode, int):
        return None
    if returncode >= 0:
        return None
    return f"SIG{-returncode}"


def _tail_file(
    path: Path, *, max_lines: int = 5, max_chars: int = 320
) -> Optional[str]:
    try:
        if not path.exists() or not path.is_file():
            return None
        size = path.stat().st_size
        start = max(0, size - _MAX_TAIL_BYTES)
        with path.open("rb") as fh:
            if start:
                fh.seek(start)
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    tail = "\n".join(lines[-max_lines:]).strip()
    if not tail:
        return None
    if len(tail) <= max_chars:
        return tail
    return tail[-max_chars:].lstrip()


def write_worker_exit_info(
    repo_root: Path,
    run_id: str,
    *,
    returncode: Optional[int],
    artifacts_root: Optional[Path] = None,
) -> None:
    """Persist worker exit status + log tails for fast postmortem debugging."""
    import time

    normalized_run_id = _normalized_run_id(run_id)
    artifacts_dir = _worker_artifacts_dir(repo_root, normalized_run_id, artifacts_root)
    metadata = {}
    try:
        metadata = json.loads(
            _worker_metadata_path(artifacts_dir).read_text(encoding="utf-8")
        )
    except Exception:
        metadata = {}
    data = {
        "run_id": normalized_run_id,
        "returncode": returncode,
        "captured_at": time.time(),
        # Guard against stale exit info: only trust it when it matches the current
        # worker.json lifecycle metadata.
        "pid": metadata.get("pid"),
        "spawned_at": metadata.get("spawned_at"),
        "stderr_tail": _tail_file(artifacts_dir / "worker.err.log"),
        "stdout_tail": _tail_file(artifacts_dir / "worker.out.log"),
    }
    try:
        _worker_exit_path(artifacts_dir).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def write_worker_crash_info(
    repo_root: Path,
    run_id: str,
    *,
    worker_pid: Optional[int] = None,
    exit_code: Optional[int] = None,
    signal: Optional[str] = None,
    last_event: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    exception: Optional[str] = None,
    stack_trace: Optional[str] = None,
    artifacts_root: Optional[Path] = None,
) -> Optional[Path]:
    try:
        normalized_run_id = _normalized_run_id(run_id)
        artifacts_dir = _worker_artifacts_dir(
            repo_root, normalized_run_id, artifacts_root
        )
    except Exception:
        return None
    if stderr_tail is None:
        stderr_tail = _tail_file(artifacts_dir / "worker.err.log")
    payload: dict[str, Any] = {
        "timestamp": _iso_now(),
        "worker_pid": worker_pid,
        "exit_code": exit_code,
        "signal": signal or _signal_from_returncode(exit_code),
        "last_event": last_event,
        "stderr_tail": stderr_tail,
        "exception": exception,
        "stack_trace": stack_trace,
    }
    crash_path = _worker_crash_path(artifacts_dir)
    try:
        crash_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        return None
    return crash_path


def read_worker_crash_info(
    repo_root: Path,
    run_id: str,
    *,
    artifacts_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    try:
        artifacts_dir = _worker_artifacts_dir(repo_root, run_id, artifacts_root)
    except Exception:
        return None
    crash_path = _worker_crash_path(artifacts_dir)
    if not crash_path.exists():
        return None
    try:
        raw = json.loads(crash_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _build_worker_cmd(entrypoint: str, run_id: str, repo_root: Path) -> list[str]:
    normalized_run_id = _normalized_run_id(run_id)
    return [
        sys.executable,
        "-m",
        entrypoint,
        "flow",
        "worker",
        "--repo",
        str(repo_root),
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


def _write_worker_metadata(
    path: Path, pid: int, cmd: list[str], repo_root: Path
) -> None:
    import time

    data = {
        "pid": pid,
        "cmd": cmd,
        "repo_root": str(repo_root.resolve()),
        "spawned_at": time.time(),
        "parent_pid": os.getppid(),
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
    exit_path = _worker_exit_path(artifacts_dir)

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
        spawned_at = data.get("spawned_at")
        if not isinstance(spawned_at, (int, float)):
            spawned_at = None
        raw_cmd = data.get("cmd") or []
        cmd = [str(part) for part in raw_cmd] if isinstance(raw_cmd, list) else []
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
            cmdline=cmd,
            artifact_path=metadata_path,
            message="missing or invalid PID",
        )

    if not _pid_is_running(pid):
        exit_code = None
        stderr_tail = None
        crash_info = None
        crash_path = _worker_crash_path(artifacts_dir)
        if exit_path.exists():
            try:
                exit_data = json.loads(exit_path.read_text(encoding="utf-8"))
                if isinstance(exit_data, dict):
                    # Only trust the cached exit info when it matches the current
                    # worker.json (avoids stale exit payloads for reused run_ids).
                    if exit_data.get("pid") == pid and (
                        spawned_at is None or exit_data.get("spawned_at") == spawned_at
                    ):
                        raw_code = exit_data.get("returncode")
                        if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                            exit_code = raw_code
                        raw_tail = exit_data.get("stderr_tail")
                        if isinstance(raw_tail, str) and raw_tail.strip():
                            stderr_tail = raw_tail.strip()
            except Exception:
                exit_code = None
                stderr_tail = None
        crash_info = read_worker_crash_info(
            repo_root, run_id, artifacts_root=artifacts_root
        )
        if isinstance(crash_info, dict):
            crash_exit = crash_info.get("exit_code")
            if exit_code is None and isinstance(crash_exit, int):
                exit_code = crash_exit
            crash_stderr = crash_info.get("stderr_tail")
            if (
                stderr_tail is None
                and isinstance(crash_stderr, str)
                and crash_stderr.strip()
            ):
                stderr_tail = crash_stderr.strip()
        if stderr_tail is None:
            stderr_tail = _tail_file(artifacts_dir / "worker.err.log")
        return FlowWorkerHealth(
            status="dead",
            pid=pid,
            cmdline=cmd,
            artifact_path=metadata_path,
            message="worker PID not running",
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            crash_path=crash_path if crash_path.exists() else None,
            crash_info=crash_info if isinstance(crash_info, dict) else None,
        )

    expected_cmd = cmd or _build_worker_cmd(entrypoint, run_id, repo_root)
    actual_cmd = _read_process_cmdline(pid)
    if actual_cmd is None:
        # Can't inspect cmdline; trust the PID check.
        return FlowWorkerHealth(
            status="alive",
            pid=pid,
            cmdline=cmd,
            artifact_path=metadata_path,
            message="worker running (cmdline unknown)",
        )

    if not _cmdline_matches(expected_cmd, actual_cmd):
        return FlowWorkerHealth(
            status="mismatch",
            pid=pid,
            cmdline=actual_cmd,
            artifact_path=metadata_path,
            message="worker PID command does not match stored metadata",
        )

    return FlowWorkerHealth(
        status="alive",
        pid=pid,
        cmdline=actual_cmd,
        artifact_path=metadata_path,
        message="worker running",
    )


def register_worker_metadata(
    repo_root: Path,
    run_id: str,
    *,
    artifacts_root: Optional[Path] = None,
    pid: Optional[int] = None,
    cmd: Optional[list[str]] = None,
    entrypoint: str = "codex_autorunner",
) -> Path:
    normalized_run_id = _normalized_run_id(run_id)
    artifacts_dir = _worker_artifacts_dir(repo_root, normalized_run_id, artifacts_root)

    resolved_pid = pid or os.getpid()
    resolved_cmd = cmd or _read_process_cmdline(resolved_pid)
    if not resolved_cmd:
        resolved_cmd = _build_worker_cmd(entrypoint, normalized_run_id, repo_root)

    _write_worker_metadata(
        _worker_metadata_path(artifacts_dir),
        resolved_pid,
        resolved_cmd,
        repo_root,
    )
    return artifacts_dir


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

    cmd = _build_worker_cmd(entrypoint, normalized_run_id, repo_root)

    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )

    _write_worker_metadata(
        _worker_metadata_path(artifacts_dir), proc.pid, cmd, repo_root
    )
    return proc, stdout_handle, stderr_handle
