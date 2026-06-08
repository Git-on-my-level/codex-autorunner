from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from ..locks import process_matches_identity
from ..update_transaction import write_update_status_projection

LOCK_CMD_HINTS = (
    "codex_autorunner.core.update_runner",
    "codex_autorunner.core.update.runner",
)
STARTUP_GRACE_SECONDS = 10.0

__all__ = (
    "LOCK_CMD_HINTS",
    "STARTUP_GRACE_SECONDS",
    "UpdateInProgressError",
    "acquire_lock",
    "lock_active",
    "read_lock",
    "read_status_with_lock_reconcile",
    "release_lock",
)


class UpdateInProgressError(RuntimeError):
    """Raised when an update is already running."""


def read_lock(lock_path: Path) -> dict[str, object] | None:
    if not lock_path.exists():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def lock_active(lock_path: Path) -> dict[str, object] | None:
    lock = read_lock(lock_path)
    if not lock:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return None
    pid = lock.get("pid")
    if isinstance(pid, int):
        pid_matches = process_matches_identity(
            pid,
            expected_cmd_substrings=LOCK_CMD_HINTS,
        )
        if pid_matches:
            return lock
    try:
        lock_path.unlink()
    except OSError:
        pass
    return None


def acquire_lock(
    lock_path: Path,
    *,
    repo_url: str,
    repo_ref: str,
    update_target: str,
    logger: logging.Logger,
) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "repo_url": repo_url,
        "repo_ref": repo_ref,
        "update_target": update_target,
    }
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        existing = lock_active(lock_path)
        if existing:
            msg = f"Update already running (pid {existing.get('pid')})."
            logger.info(msg)
            raise UpdateInProgressError(msg) from exc
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            msg = "Update already running."
            logger.info(msg)
            raise UpdateInProgressError(msg) from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload))
    return True


def release_lock(lock_path: Path) -> None:
    lock = read_lock(lock_path)
    if not lock or lock.get("pid") != os.getpid():
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


def read_status_with_lock_reconcile(
    status_path: Path,
    lock_path: Path,
) -> dict[str, object] | None:
    if not status_path.exists():
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if status in ("running", "spawned") and lock_active(lock_path) is None:
        started_at = payload.get("at")
        if (
            isinstance(started_at, (int, float))
            and (time.time() - float(started_at)) < STARTUP_GRACE_SECONDS
        ):
            return payload
        write_update_status_projection(
            status_path,
            status="error",
            message="Update not running; last update may have crashed.",
            extra={"previous_status": status},
        )
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    return payload
