import json
from pathlib import Path

from codex_autorunner.core.locks import assess_lock, read_lock_info, write_lock_info


def test_read_lock_info_pid_text(tmp_path: Path) -> None:
    lock_path = tmp_path / "lock"
    lock_path.write_text("12345", encoding="utf-8")
    info = read_lock_info(lock_path)
    assert info.pid == 12345
    assert info.started_at is None


def test_write_lock_info_roundtrip(tmp_path: Path) -> None:
    lock_path = tmp_path / "lock"
    write_lock_info(lock_path, 999, started_at="2025-01-01T00:00:00Z")
    info = read_lock_info(lock_path)
    assert info.pid == 999
    assert info.started_at == "2025-01-01T00:00:00Z"
    assert info.host


def test_assess_lock_different_host_dead_pid_freeable(tmp_path: Path) -> None:
    """Lock from different host with dead PID should be freeable."""
    lock_path = tmp_path / "lock"
    other_host = "different-host"
    payload = {
        "pid": 999999,
        "started_at": "2025-01-01T00:00:00Z",
        "host": other_host,
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    assessment = assess_lock(lock_path, require_host_match=True)

    assert assessment.freeable is True
    assert assessment.reason == "Lock pid is not running; safe to clear."
    assert assessment.pid == 999999
    assert assessment.host == other_host


def test_assess_lock_different_host_live_pid_not_freeable(tmp_path: Path) -> None:
    """Lock from different host with live PID should not be freeable."""
    import os

    lock_path = tmp_path / "lock"
    other_host = "different-host"
    current_pid = os.getpid()
    payload = {
        "pid": current_pid,
        "started_at": "2025-01-01T00:00:00Z",
        "host": other_host,
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    assessment = assess_lock(lock_path, require_host_match=True)

    assert assessment.freeable is False
    assert assessment.reason == "Lock belongs to another host."
    assert assessment.pid == current_pid
    assert assessment.host == other_host
