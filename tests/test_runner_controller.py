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


def test_reconcile_run_index_stale_entries(repo: Path, monkeypatch) -> None:
    engine = Engine(repo)
    state = load_state(engine.state_path)
    state.status = "idle"
    state.runner_pid = None
    state.last_run_id = 2
    state.last_exit_code = 1
    save_state(engine.state_path, state)

    engine._run_index_store.merge_entry(1, {"started_at": "2025-01-01T00:00:00Z"})
    engine._run_index_store.merge_entry(2, {"started_at": "2025-01-01T01:00:00Z"})

    monkeypatch.setattr(
        "codex_autorunner.core.locks.process_alive",
        lambda _pid: False,
    )

    engine.reconcile_run_index()

    index = engine._load_run_index()
    entry_1 = index.get("1", {})
    entry_2 = index.get("2", {})

    assert entry_1.get("finished_at") is not None
    assert entry_1.get("exit_code") == 1
    assert entry_1.get("reconciled_at") is not None
    assert entry_1.get("reconciled_reason") == "runner_inactive"

    assert entry_2.get("finished_at") is not None
    assert entry_2.get("exit_code") == 1
    assert entry_2.get("reconciled_at") is not None
    assert entry_2.get("reconciled_reason") == "runner_inactive"


def test_reconcile_run_index_active_run_not_modified(repo: Path, monkeypatch) -> None:
    from codex_autorunner.core import engine

    engine_instance = Engine(repo)
    state = load_state(engine_instance.state_path)
    state.status = "running"
    state.runner_pid = 12345
    state.last_run_id = 3
    state.last_exit_code = None
    save_state(engine_instance.state_path, state)

    engine_instance._run_index_store.merge_entry(
        1, {"started_at": "2025-01-01T00:00:00Z"}
    )
    engine_instance._run_index_store.merge_entry(
        2, {"started_at": "2025-01-01T01:00:00Z"}
    )
    engine_instance._run_index_store.merge_entry(
        3, {"started_at": "2025-01-01T02:00:00Z"}
    )

    monkeypatch.setattr(engine, "process_alive", lambda _pid: True)

    engine_instance.reconcile_run_index()

    index = engine_instance._load_run_index()
    entry_1 = index.get("1", {})
    entry_2 = index.get("2", {})
    entry_3 = index.get("3", {})

    assert entry_1.get("finished_at") is not None
    assert entry_1.get("exit_code") == 1

    assert entry_2.get("finished_at") is not None
    assert entry_2.get("exit_code") == 1

    assert entry_3.get("finished_at") is None
    assert entry_3.get("exit_code") is None
    assert entry_3.get("reconciled_at") is None


def test_reconcile_run_index_already_reconciled_not_modified(
    repo: Path, monkeypatch
) -> None:
    engine = Engine(repo)
    state = load_state(engine.state_path)
    state.status = "idle"
    state.runner_pid = None
    state.last_run_id = 1
    save_state(engine.state_path, state)

    engine._run_index_store.merge_entry(
        1,
        {
            "started_at": "2025-01-01T00:00:00Z",
            "finished_at": "2025-01-01T00:05:00Z",
            "exit_code": 0,
            "reconciled_at": "2025-01-01T00:10:00Z",
        },
    )

    monkeypatch.setattr(
        "codex_autorunner.core.locks.process_alive",
        lambda _pid: False,
    )

    original_finished_at = engine._load_run_index().get("1", {}).get("finished_at")
    engine.reconcile_run_index()

    index = engine._load_run_index()
    entry = index.get("1", {})

    assert entry.get("finished_at") == original_finished_at
    assert entry.get("exit_code") == 0


def test_reconcile_run_index_completed_entries_not_modified(
    repo: Path, monkeypatch
) -> None:
    engine = Engine(repo)
    state = load_state(engine.state_path)
    state.status = "idle"
    state.runner_pid = None
    state.last_run_id = 2
    save_state(engine.state_path, state)

    engine._run_index_store.merge_entry(
        1,
        {
            "started_at": "2025-01-01T00:00:00Z",
            "finished_at": "2025-01-01T00:05:00Z",
            "exit_code": 0,
        },
    )

    monkeypatch.setattr(
        "codex_autorunner.core.locks.process_alive",
        lambda _pid: False,
    )

    original_finished_at = engine._load_run_index().get("1", {}).get("finished_at")
    engine.reconcile_run_index()

    index = engine._load_run_index()
    entry = index.get("1", {})

    assert entry.get("finished_at") == original_finished_at
    assert entry.get("exit_code") == 0
    assert entry.get("reconciled_at") is None


def test_reconcile_run_index_uses_last_exit_code(repo: Path, monkeypatch) -> None:
    engine = Engine(repo)
    state = load_state(engine.state_path)
    state.status = "idle"
    state.runner_pid = None
    state.last_run_id = 2
    state.last_exit_code = 42
    save_state(engine.state_path, state)

    engine._run_index_store.merge_entry(1, {"started_at": "2025-01-01T00:00:00Z"})
    engine._run_index_store.merge_entry(2, {"started_at": "2025-01-01T01:00:00Z"})

    monkeypatch.setattr(
        "codex_autorunner.core.locks.process_alive",
        lambda _pid: False,
    )

    engine.reconcile_run_index()

    index = engine._load_run_index()
    entry_1 = index.get("1", {})
    entry_2 = index.get("2", {})

    assert entry_1.get("finished_at") is not None
    assert entry_1.get("exit_code") == 1

    assert entry_2.get("finished_at") is not None
    assert entry_2.get("exit_code") == 42


def test_reconcile_run_index_runner_active_different_run(
    repo: Path, monkeypatch
) -> None:
    from codex_autorunner.core import engine as engine_module

    engine_instance = Engine(repo)
    state = load_state(engine_instance.state_path)
    state.status = "running"
    state.runner_pid = 12345
    state.last_run_id = 3
    state.last_exit_code = None
    save_state(engine_instance.state_path, state)

    engine_instance._run_index_store.merge_entry(
        1, {"started_at": "2025-01-01T00:00:00Z"}
    )
    engine_instance._run_index_store.merge_entry(
        2, {"started_at": "2025-01-01T01:00:00Z"}
    )
    engine_instance._run_index_store.merge_entry(
        3, {"started_at": "2025-01-01T02:00:00Z"}
    )

    monkeypatch.setattr(engine_module, "process_alive", lambda _pid: True)

    engine_instance.reconcile_run_index()

    index = engine_instance._load_run_index()
    entry_1 = index.get("1", {})
    entry_2 = index.get("2", {})
    entry_3 = index.get("3", {})

    assert entry_1.get("finished_at") is not None
    assert entry_1.get("exit_code") == 1
    assert entry_1.get("reconciled_reason") == "runner_active"

    assert entry_2.get("finished_at") is not None
    assert entry_2.get("exit_code") == 1
    assert entry_2.get("reconciled_reason") == "runner_active"

    assert entry_3.get("finished_at") is None
    assert entry_3.get("exit_code") is None
    assert entry_3.get("reconciled_at") is None
