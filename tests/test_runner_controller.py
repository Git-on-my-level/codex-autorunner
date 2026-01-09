from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.engine import Engine
from codex_autorunner.core.runner_controller import ProcessRunnerController
from codex_autorunner.core.state import load_state, save_state


def test_reconcile_clears_stale_runner_pid(repo: Path, monkeypatch) -> None:
    engine = Engine(repo)
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
    engine = Engine(repo)
    calls: list[list[str]] = []

    def fake_spawn(cmd: list[str], _engine: Engine) -> None:
        calls.append(cmd)

    controller = ProcessRunnerController(engine, spawn_fn=fake_spawn)
    controller.start(once=True)
    controller.resume(once=True)

    assert calls[0][3] == "once"
    assert calls[1][3] == "resume"
    assert calls[1][-1] == "--once"
