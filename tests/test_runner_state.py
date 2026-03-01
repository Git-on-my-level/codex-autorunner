from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.locks import LockAssessment
from codex_autorunner.core.runner_state import RunnerStateManager
from codex_autorunner.core.state import load_state, save_state


def test_repo_busy_reason_with_stale_state_no_pid(repo: Path, monkeypatch) -> None:
    """State says running but no pid - should not report busy."""
    manager = RunnerStateManager(repo)
    state = load_state(manager.state_path)
    state.status = "running"
    state.runner_pid = None
    save_state(manager.state_path, state)

    busy_reason = manager.repo_busy_reason()
    assert busy_reason == "Autorunner state is stale; use 'car resume' to continue."


def test_repo_busy_reason_with_stale_state_dead_pid(repo: Path, monkeypatch) -> None:
    """State says running with dead pid - should not report busy."""
    manager = RunnerStateManager(repo)
    state = load_state(manager.state_path)
    state.status = "running"
    state.runner_pid = 99999
    save_state(manager.state_path, state)

    monkeypatch.setattr(
        "codex_autorunner.core.runner_state.process_matches_identity",
        lambda _pid, **_kwargs: False,
    )

    busy_reason = manager.repo_busy_reason()
    assert busy_reason == "Autorunner state is stale; use 'car resume' to continue."


def test_repo_busy_reason_with_live_pid(repo: Path, monkeypatch) -> None:
    """State says running with live pid - should report busy."""
    import os

    manager = RunnerStateManager(repo)
    current_pid = os.getpid()
    state = load_state(manager.state_path)
    state.status = "running"
    state.runner_pid = current_pid
    save_state(manager.state_path, state)

    monkeypatch.setattr(
        "codex_autorunner.core.runner_state.process_matches_identity",
        lambda _pid, **_kwargs: _pid == current_pid,
    )

    busy_reason = manager.repo_busy_reason()
    assert (
        busy_reason
        == f"Autorunner is currently running (pid={current_pid}); try again later."
    )


def test_repo_busy_reason_not_running(repo: Path) -> None:
    """State is idle - should not report busy."""
    manager = RunnerStateManager(repo)
    state = load_state(manager.state_path)
    state.status = "idle"
    state.runner_pid = None
    save_state(manager.state_path, state)

    busy_reason = manager.repo_busy_reason()
    assert busy_reason is None


def test_repo_busy_reason_with_lock(repo: Path, monkeypatch) -> None:
    """Lock file exists with live pid - should report busy."""
    import os

    manager = RunnerStateManager(repo)
    current_pid = os.getpid()
    lock_payload = {
        "pid": current_pid,
        "host": "localhost",
        "started_at": "2025-01-01T00:00:00Z",
    }
    manager.lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")

    monkeypatch.setattr(
        "codex_autorunner.core.runner_state.assess_lock",
        lambda _path, **_kwargs: LockAssessment(
            freeable=False, reason=None, pid=current_pid, host="localhost"
        ),
    )
    busy_reason = manager.repo_busy_reason()
    assert (
        busy_reason
        == f"Autorunner is running (pid={current_pid} on localhost); try again later."
    )


def test_kill_running_process_skips_pid_identity_mismatch(
    repo: Path, monkeypatch
) -> None:
    manager = RunnerStateManager(repo)
    manager.lock_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    kill_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        "codex_autorunner.core.runner_state.process_matches_identity",
        lambda _pid, **_kwargs: False,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runner_state.os.kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    assert manager.kill_running_process() is None
    assert kill_calls == []
    assert not manager.lock_path.exists()
