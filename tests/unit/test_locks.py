import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from codex_autorunner.core.locks import (
    FileLock,
    FileLockBusy,
    FileLockError,
    LockAssessment,
    LockInfo,
    assess_lock,
    file_lock,
    process_alive,
    process_command,
    process_is_active,
    process_is_zombie,
    read_lock_info,
    write_lock_info,
)


class TestProcessHelpers:
    def test_process_alive_current_pid(self) -> None:
        assert process_alive(os.getpid()) is True

    def test_process_alive_dead_pid(self) -> None:
        assert process_alive(9999999) is False

    def test_process_is_zombie_current_pid(self) -> None:
        assert process_is_zombie(os.getpid()) is False

    def test_process_is_active_current_pid(self) -> None:
        assert process_is_active(os.getpid()) is True

    def test_process_command_current_pid(self) -> None:
        cmd = process_command(os.getpid())
        if os.name != "nt":
            assert cmd is not None
            assert "python" in cmd.lower() or "pytest" in cmd.lower()


class TestReadLockInfo:
    def test_read_lock_info_missing_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "nonexistent.lock"
        info = read_lock_info(lock_path)
        assert info.pid is None
        assert info.started_at is None
        assert info.host is None

    def test_read_lock_info_empty_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("", encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid is None
        assert info.started_at is None
        assert info.host is None

    def test_read_lock_info_whitespace_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("   \n  ", encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid is None
        assert info.started_at is None
        assert info.host is None

    def test_read_lock_info_pid_only(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("12345", encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid == 12345
        assert info.started_at is None
        assert info.host is None

    def test_read_lock_info_invalid_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("not-a-pid", encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid is None
        assert info.started_at is None
        assert info.host is None

    def test_read_lock_info_json_format(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        payload = {
            "pid": 54321,
            "started_at": "2025-01-15T10:30:00Z",
            "host": "testhost",
        }
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid == 54321
        assert info.started_at == "2025-01-15T10:30:00Z"
        assert info.host == "testhost"

    def test_read_lock_info_json_partial(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text('{"pid": "999"}', encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid == 999
        assert info.started_at is None
        assert info.host is None

    def test_read_lock_info_json_invalid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text('{"pid": "not-a-number"}', encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid is None

    def test_read_lock_info_malformed_json(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("{invalid json", encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid is None
        assert info.started_at is None
        assert info.host is None


class TestWriteLockInfo:
    def test_write_lock_info_creates_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "subdir" / "lock"
        write_lock_info(lock_path, 12345, started_at="2025-01-15T10:00:00Z")
        assert lock_path.exists()

    def test_write_lock_info_content(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        write_lock_info(lock_path, 12345, started_at="2025-01-15T10:00:00Z")
        content = lock_path.read_text(encoding="utf-8")
        payload = json.loads(content)
        assert payload["pid"] == 12345
        assert payload["started_at"] == "2025-01-15T10:00:00Z"
        assert payload["host"] == socket.gethostname()

    def test_write_lock_info_roundtrip(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        write_lock_info(lock_path, 99999, started_at="2025-06-15T14:30:00Z")
        info = read_lock_info(lock_path)
        assert info.pid == 99999
        assert info.started_at == "2025-06-15T14:30:00Z"
        assert info.host == socket.gethostname()


class TestFileLockAcquireRelease:
    def test_acquire_creates_parent_dirs(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "deeply" / "nested" / "dir" / "lock"
        fl = FileLock(lock_path)
        fl.acquire()
        try:
            assert lock_path.parent.exists()
        finally:
            fl.release()

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        fl.acquire()
        assert fl._file is not None
        fl.release()
        assert fl._file is None

    def test_release_without_acquire(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        fl.release()
        assert fl._file is None

    def test_double_acquire_is_noop(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        fl.acquire()
        first_file = fl._file
        fl.acquire()
        assert fl._file is first_file
        fl.release()

    def test_acquire_non_blocking_available(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        fl.acquire(blocking=False)
        assert fl._file is not None
        fl.release()

    def test_acquire_non_blocking_already_locked(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl1 = FileLock(lock_path)
        fl1.acquire()
        try:
            fl2 = FileLock(lock_path)
            with pytest.raises(FileLockBusy):
                fl2.acquire(blocking=False)
        finally:
            fl1.release()

    def test_acquire_blocking_waits(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        acquired_after_release = threading.Event()
        first_holder_done = threading.Event()

        def first_holder() -> None:
            fl = FileLock(lock_path)
            fl.acquire()
            try:
                time.sleep(0.2)
            finally:
                fl.release()
                first_holder_done.set()

        def second_waiter() -> None:
            fl = FileLock(lock_path)
            fl.acquire(blocking=True)
            try:
                acquired_after_release.set()
            finally:
                fl.release()

        t1 = threading.Thread(target=first_holder)
        t2 = threading.Thread(target=second_waiter)
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=1.0)
        t2.join(timeout=1.0)
        assert acquired_after_release.is_set()
        assert first_holder_done.is_set()


class TestFileLockContextManager:
    def test_context_manager(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with FileLock(lock_path) as fl:
            assert fl._file is not None
        assert fl._file is None

    def test_context_manager_with_exception(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        try:
            with fl:
                raise ValueError("test error")
        except ValueError:
            pass
        assert fl._file is None


class TestFileLockWriteText:
    def test_write_text(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with FileLock(lock_path) as fl:
            fl.write_text("test content")
        assert lock_path.read_text(encoding="utf-8") == "test content"

    def test_write_text_truncates(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with FileLock(lock_path) as fl:
            fl.write_text("initial long content")
            fl.write_text("short")
        assert lock_path.read_text(encoding="utf-8") == "short"

    def test_write_text_without_acquire(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        with pytest.raises(FileLockError):
            fl.write_text("test")


class TestFileLockFunction:
    def test_file_lock_context_manager(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with file_lock(lock_path):
            fl2 = FileLock(lock_path)
            with pytest.raises(FileLockBusy):
                fl2.acquire(blocking=False)

    def test_file_lock_non_blocking(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with file_lock(lock_path, blocking=True):
            with pytest.raises(FileLockBusy):
                file_lock(lock_path, blocking=False).__enter__()

    def test_file_lock_exception_propagates(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with pytest.raises(RuntimeError):
            with file_lock(lock_path):
                raise RuntimeError("test")


class TestAssessLock:
    def test_assess_lock_missing_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "nonexistent.lock"
        assessment = assess_lock(lock_path)
        assert assessment.freeable is False
        assert assessment.reason is None
        assert assessment.pid is None
        assert assessment.host is None

    def test_assess_lock_no_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text('{"started_at": "2025-01-01T00:00:00Z"}', encoding="utf-8")
        assessment = assess_lock(lock_path)
        assert assessment.freeable is True
        assert "no pid" in assessment.reason.lower()
        assert assessment.pid is None

    def test_assess_lock_dead_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        payload = {"pid": 9999999, "started_at": "2025-01-01T00:00:00Z", "host": "test"}
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        assessment = assess_lock(lock_path)
        assert assessment.freeable is True
        assert "not running" in assessment.reason.lower()
        assert assessment.pid == 9999999

    def test_assess_lock_different_host_alive_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        payload = {
            "pid": os.getpid(),
            "started_at": "2025-01-01T00:00:00Z",
            "host": "different-host",
        }
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        assessment = assess_lock(lock_path, require_host_match=True)
        assert assessment.freeable is False
        assert "another host" in assessment.reason.lower()
        assert assessment.pid == os.getpid()

    def test_assess_lock_different_host_no_match_required(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        payload = {
            "pid": os.getpid(),
            "started_at": "2025-01-01T00:00:00Z",
            "host": "different-host",
        }
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        assessment = assess_lock(lock_path, require_host_match=False)
        assert assessment.freeable is False

    def test_assess_lock_same_host_alive_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        payload = {
            "pid": os.getpid(),
            "started_at": "2025-01-01T00:00:00Z",
            "host": socket.gethostname(),
        }
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        assessment = assess_lock(lock_path, require_host_match=True)
        assert assessment.freeable is False
        assert assessment.reason is None

    def test_assess_lock_cmd_mismatch(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("Command inspection not available on Windows")
        lock_path = tmp_path / "lock"
        payload = {
            "pid": os.getpid(),
            "started_at": "2025-01-01T00:00:00Z",
            "host": socket.gethostname(),
        }
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        assessment = assess_lock(
            lock_path,
            expected_cmd_substrings=["nonexistent_cmd_pattern_xyz"],
            require_host_match=False,
        )
        assert assessment.freeable is True
        assert "does not match" in assessment.reason.lower()

    @pytest.mark.slow
    def test_assess_lock_cmd_match(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("Command inspection not available on Windows")
        lock_path = tmp_path / "lock"
        payload = {
            "pid": os.getpid(),
            "started_at": "2025-01-01T00:00:00Z",
            "host": socket.gethostname(),
        }
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        assessment = assess_lock(
            lock_path,
            expected_cmd_substrings=["python", "pytest"],
            require_host_match=False,
        )
        assert assessment.freeable is False


class TestEdgeCases:
    def test_lock_file_corruption_truncated_json(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text('{"pid": 12', encoding="utf-8")
        info = read_lock_info(lock_path)
        assert info.pid is None

    def test_lock_file_corruption_binary(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_bytes(b"\xff\xfe invalid utf8 \x80\x81")
        with pytest.raises(UnicodeDecodeError):
            read_lock_info(lock_path)

    def test_lock_file_corruption_unicode(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("\ufffd\ufffe\uffff", encoding="utf-8", errors="replace")
        info = read_lock_info(lock_path)
        assert info.pid is None

    @pytest.mark.slow
    def test_concurrent_access_multiple_threads(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        success_count = 0
        lock = threading.Lock()

        def try_acquire() -> None:
            nonlocal success_count
            fl = FileLock(lock_path)
            try:
                fl.acquire(blocking=True)
                time.sleep(0.05)
                with lock:
                    success_count += 1
            finally:
                fl.release()

        threads = [threading.Thread(target=try_acquire) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert success_count == 5

    def test_write_lock_info_with_file_handle(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        with FileLock(lock_path) as fl:
            write_lock_info(
                lock_path,
                os.getpid(),
                started_at="2025-01-15T10:00:00Z",
                lock_file=fl.file,
            )
        info = read_lock_info(lock_path)
        assert info.pid == os.getpid()
        assert info.started_at == "2025-01-15T10:00:00Z"

    def test_file_property_raises_when_not_acquired(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        fl = FileLock(lock_path)
        with pytest.raises(FileLockError, match="not acquired"):
            _ = fl.file

    def test_overwrite_existing_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.write_text("old content", encoding="utf-8")
        write_lock_info(lock_path, 12345, started_at="2025-01-15T10:00:00Z")
        info = read_lock_info(lock_path)
        assert info.pid == 12345


class TestDataClasses:
    def test_lock_info_dataclass(self) -> None:
        info = LockInfo(pid=123, started_at="2025-01-01", host="testhost")
        assert info.pid == 123
        assert info.started_at == "2025-01-01"
        assert info.host == "testhost"

    def test_lock_assessment_dataclass(self) -> None:
        assessment = LockAssessment(
            freeable=True, reason="test reason", pid=456, host="testhost"
        )
        assert assessment.freeable is True
        assert assessment.reason == "test reason"
        assert assessment.pid == 456
        assert assessment.host == "testhost"
