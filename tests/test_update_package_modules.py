from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autorunner.core.update.lock import (
    STARTUP_GRACE_SECONDS,
    UpdateInProgressError,
    acquire_lock,
    lock_active,
    read_lock,
    read_status_with_lock_reconcile,
    release_lock,
)
from codex_autorunner.core.update.source import (
    cache_refresh_failure_is_retryable,
    cleanup_build_artifacts,
    refresh_failure_is_retryable,
)
from codex_autorunner.core.update.status import StatusReporter


def test_status_reporter_write_preserves_notify_keys(tmp_path: Path) -> None:
    status_path = tmp_path / "update_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "message": "old",
                "notify_platform": "telegram",
                "notify_chat_id": 123,
            }
        ),
        encoding="utf-8",
    )

    reporter = StatusReporter(status_path, run_id="run-1")
    payload = reporter.write("running", "Update started.", phase="worker_start")

    assert payload["status"] == "running"
    assert payload["phase"] == "worker_start"
    assert payload["update_run_id"] == "run-1"
    assert payload["notify_platform"] == "telegram"
    assert payload["notify_chat_id"] == 123


def test_status_reporter_log_phase_timing_preserves_status(tmp_path: Path) -> None:
    status_path = tmp_path / "update_status.json"
    status_path.write_text(
        json.dumps({"status": "running", "message": "Working.", "at": 1.0}),
        encoding="utf-8",
    )

    reporter = StatusReporter(status_path, run_id="run-2")
    payload = reporter.log_phase_timing("pip_install", "ok", 1500.0)

    assert payload["status"] == "running"
    assert payload["message"] == "Working."
    assert payload["last_phase_timing"]["phase"] == "pip_install"
    assert payload["last_phase_timing"]["status"] == "ok"
    assert payload["last_phase_timing"]["duration_ms"] == 1500
    assert payload["last_phase_timing"]["update_run_id"] == "run-2"


def test_status_reporter_timed_phase_records_ok_and_failed(tmp_path: Path) -> None:
    status_path = tmp_path / "update_status.json"
    reporter = StatusReporter(status_path)

    with reporter.timed_phase("hub_restart"):
        pass

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["last_phase_timing"]["phase"] == "hub_restart"
    assert payload["last_phase_timing"]["status"] == "ok"

    with pytest.raises(ValueError, match="boom"):
        with reporter.timed_phase("pip_install"):
            raise ValueError("boom")

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["last_phase_timing"]["phase"] == "pip_install"
    assert payload["last_phase_timing"]["status"] == "failed"


def test_acquire_and_release_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "update.lock"
    logger = logging.getLogger("test_update_lock")

    acquire_lock(
        lock_path,
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_target="all",
        logger=logger,
    )

    lock = read_lock(lock_path)
    assert lock is not None
    assert lock["repo_url"] == "https://example.com/repo.git"
    assert lock["repo_ref"] == "main"
    assert lock["update_target"] == "all"
    with patch(
        "codex_autorunner.core.update.lock.process_matches_identity",
        return_value=True,
    ):
        assert lock_active(lock_path) is not None

    release_lock(lock_path)
    assert not lock_path.exists()


def test_acquire_lock_raises_when_active(tmp_path: Path) -> None:
    lock_path = tmp_path / "update.lock"
    logger = logging.getLogger("test_update_lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": 999999, "started_at": 1.0}),
        encoding="utf-8",
    )

    with patch(
        "codex_autorunner.core.update.lock.process_matches_identity",
        return_value=True,
    ):
        with pytest.raises(UpdateInProgressError, match="already running"):
            acquire_lock(
                lock_path,
                repo_url="https://example.com/repo.git",
                repo_ref="main",
                update_target="web",
                logger=logger,
            )


def test_read_status_with_lock_reconcile_marks_stale_running(tmp_path: Path) -> None:
    status_path = tmp_path / "update_status.json"
    lock_path = tmp_path / "update.lock"
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "message": "Update started.",
                "at": 1.0,
            }
        ),
        encoding="utf-8",
    )

    payload = read_status_with_lock_reconcile(status_path, lock_path)

    assert payload is not None
    assert payload["status"] == "error"
    assert payload["previous_status"] == "running"


def test_read_status_with_lock_reconcile_respects_startup_grace(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "update_status.json"
    lock_path = tmp_path / "update.lock"
    import time

    status_path.write_text(
        json.dumps(
            {
                "status": "spawned",
                "message": "Update spawned.",
                "at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    payload = read_status_with_lock_reconcile(status_path, lock_path)

    assert payload is not None
    assert payload["status"] == "spawned"
    assert STARTUP_GRACE_SECONDS == 10.0


def test_refresh_failure_is_retryable() -> None:
    assert refresh_failure_is_retryable(
        [
            "Building wheel for codex-autorunner",
            "error: [Errno 2] No such file or directory: 'build/lib/foo'",
        ]
    )
    assert not refresh_failure_is_retryable(["unrelated error"])


def test_cache_refresh_failure_is_retryable() -> None:
    assert cache_refresh_failure_is_retryable("fatal: index file corrupt")
    assert not cache_refresh_failure_is_retryable("permission denied")


def test_cleanup_build_artifacts(tmp_path: Path) -> None:
    update_dir = tmp_path / "cache"
    update_dir.mkdir()
    (update_dir / "build").mkdir()
    (update_dir / "dist").mkdir()
    (update_dir / "codex_autorunner.egg-info").mkdir()

    logger = logging.getLogger("test_update_source")
    removed = cleanup_build_artifacts(update_dir, logger)

    assert "build" in removed
    assert "dist" in removed
    assert not (update_dir / "build").exists()
    assert not (update_dir / "dist").exists()
