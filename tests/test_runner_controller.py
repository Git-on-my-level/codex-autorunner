from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.locks import LockAssessment
from codex_autorunner.core.runner_controller import ProcessRunnerController
from codex_autorunner.core.runner_state import LockError
from codex_autorunner.core.runtime import RuntimeContext
from codex_autorunner.core.state import load_state, save_state


def test_reconcile_clears_stale_runner_pid(repo: Path, monkeypatch) -> None:
    engine = RuntimeContext(repo)
    state = load_state(engine.state_path)
    state.status = "running"
    state.runner_pid = 99999
    state.last_exit_code = None
    save_state(engine.state_path, state)

    monkeypatch.setattr(
        "codex_autorunner.core.runner_controller.process_alive",
        lambda _pid: False,
    )

    controller = ProcessRunnerController(engine)
    controller.reconcile()

    updated = load_state(engine.state_path)
    assert updated.runner_pid is None
    assert updated.status == "error"
    assert updated.last_exit_code == 1
    assert updated.last_run_finished_at is not None


def test_start_and_resume_spawn_commands(repo: Path) -> None:
    engine = RuntimeContext(repo)
    calls: list[list[str]] = []

    def fake_spawn(cmd: list[str], _ctx: RuntimeContext) -> None:
        calls.append(cmd)

    controller = ProcessRunnerController(engine, spawn_fn=fake_spawn)
    controller.start(once=True)
    controller.resume(once=True)

    assert calls[0][3] == "once"
    assert calls[1][3] == "resume"
    assert calls[1][-1] == "--once"


def test_start_raises_when_active_lock(monkeypatch, repo: Path) -> None:
    ctx = RuntimeContext(repo)
    lock_payload = {
        "pid": 12345,
        "host": "localhost",
        "started_at": "2025-01-01T00:00:00Z",
    }
    ctx.lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")

    monkeypatch.setattr(
        "codex_autorunner.core.locks.assess_lock",
        lambda _path, **_kwargs: LockAssessment(
            freeable=False, reason=None, pid=12345, host="localhost"
        ),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.locks.process_alive",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runner_controller.process_alive",
        lambda _pid: True,
    )

    controller = ProcessRunnerController(ctx)
    with pytest.raises(LockError):
        controller.start()
