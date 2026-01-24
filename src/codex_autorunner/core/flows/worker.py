from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Optional, Tuple

from ..locks import process_is_active
from ..logging_utils import safe_log
from ..utils import atomic_write

_ACTIVE_WORKERS: dict[
    str, Tuple[subprocess.Popen, Optional[IO[bytes]], Optional[IO[bytes]]]
] = {}


def _worker_key(repo_root: Path, run_id: str) -> str:
    return f"{repo_root.resolve()}::{run_id}"


def flow_worker_dir(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".codex-autorunner" / "flows" / run_id


def flow_worker_log_paths(repo_root: Path, run_id: str) -> tuple[Path, Path]:
    base_dir = flow_worker_dir(repo_root, run_id)
    return base_dir / "worker.out.log", base_dir / "worker.err.log"


def flow_worker_pid_path(repo_root: Path, run_id: str) -> Path:
    return flow_worker_dir(repo_root, run_id) / "worker.pid"


def _read_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid(path: Path, pid: int, logger: Optional[logging.Logger]) -> None:
    try:
        atomic_write(path, f"{pid}\n")
    except Exception as exc:
        if logger is not None:
            safe_log(
                logger,
                logging.WARNING,
                "Failed to write flow worker pid file %s",
                path,
                exc=exc,
            )


def _cleanup_worker_handle(repo_root: Path, run_id: str) -> None:
    key = _worker_key(repo_root, run_id)
    handle = _ACTIVE_WORKERS.pop(key, None)
    if not handle:
        return
    proc, stdout, stderr = handle
    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    for stream in (stdout, stderr):
        if stream and not stream.closed:
            try:
                stream.flush()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


def _clear_stale_pid(repo_root: Path, run_id: str) -> None:
    pid_path = flow_worker_pid_path(repo_root, run_id)
    try:
        pid_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def reap_flow_worker(repo_root: Path, run_id: str) -> None:
    key = _worker_key(repo_root, run_id)
    handle = _ACTIVE_WORKERS.get(key)
    if not handle:
        return
    proc, *_ = handle
    if proc.poll() is not None:
        _cleanup_worker_handle(repo_root, run_id)
        _clear_stale_pid(repo_root, run_id)


def spawn_flow_worker(
    repo_root: Path,
    run_id: str,
    *,
    logger: Optional[logging.Logger] = None,
    start_new_session: bool = False,
) -> Optional[subprocess.Popen]:
    key = _worker_key(repo_root, run_id)
    handle = _ACTIVE_WORKERS.get(key)
    if handle:
        proc, *_ = handle
        if proc.poll() is None:
            if logger is not None:
                safe_log(
                    logger,
                    logging.INFO,
                    "Worker already active for run %s, skipping spawn",
                    run_id,
                )
            return None
        _cleanup_worker_handle(repo_root, run_id)

    pid_path = flow_worker_pid_path(repo_root, run_id)
    existing_pid = _read_pid(pid_path)
    if existing_pid is not None and process_is_active(existing_pid):
        if logger is not None:
            safe_log(
                logger,
                logging.INFO,
                "Worker already active for run %s (pid=%d), skipping spawn",
                run_id,
                existing_pid,
            )
        return None
    if existing_pid is not None:
        _clear_stale_pid(repo_root, run_id)

    log_out_path, log_err_path = flow_worker_log_paths(repo_root, run_id)
    log_out_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = log_out_path.open("ab")
    stderr_handle = log_err_path.open("ab")

    cmd = [
        sys.executable,
        "-m",
        "codex_autorunner.cli",
        "flow",
        "worker",
        "--run-id",
        run_id,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=start_new_session,
        )
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        raise

    _ACTIVE_WORKERS[key] = (proc, stdout_handle, stderr_handle)
    _write_pid(pid_path, proc.pid, logger)
    if logger is not None:
        safe_log(
            logger,
            logging.INFO,
            "Started flow worker for run %s (pid=%d)",
            run_id,
            proc.pid,
        )
    return proc


def stop_flow_worker(
    repo_root: Path,
    run_id: str,
    *,
    timeout: float = 10.0,
    logger: Optional[logging.Logger] = None,
) -> None:
    key = _worker_key(repo_root, run_id)
    handle = _ACTIVE_WORKERS.get(key)
    if handle:
        proc, *_ = handle
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if logger is not None:
                    safe_log(
                        logger,
                        logging.WARNING,
                        "Worker for run %s did not exit in time, killing",
                        run_id,
                    )
                proc.kill()
            except Exception as exc:
                if logger is not None:
                    safe_log(
                        logger,
                        logging.WARNING,
                        "Error stopping worker %s",
                        run_id,
                        exc=exc,
                    )
        _cleanup_worker_handle(repo_root, run_id)
        _clear_stale_pid(repo_root, run_id)
        return

    pid_path = flow_worker_pid_path(repo_root, run_id)
    pid = _read_pid(pid_path)
    if pid is None or not process_is_active(pid):
        _clear_stale_pid(repo_root, run_id)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_active(pid):
            _clear_stale_pid(repo_root, run_id)
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return
    _clear_stale_pid(repo_root, run_id)
