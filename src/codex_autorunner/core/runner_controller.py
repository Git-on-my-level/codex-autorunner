from __future__ import annotations

import threading
from collections.abc import Callable

from .engine import Engine, LockError
from .locks import process_alive, read_lock_info
from .runner_process import build_runner_cmd, spawn_detached
from .state import RunnerState, load_state, now_iso, save_state, state_lock

SpawnRunnerFn = Callable[[list[str], Engine], object]


def _spawn_detached(cmd: list[str], engine: Engine) -> object:
    return spawn_detached(cmd, cwd=engine.repo_root)


class ProcessRunnerController:
    def __init__(self, engine: Engine, *, spawn_fn: SpawnRunnerFn | None = None):
        self.engine = engine
        self._spawn_fn = spawn_fn or _spawn_detached
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.engine.runner_pid() is not None

    def reconcile(self) -> None:
        lock_pid = None
        if self.engine.lock_path.exists():
            info = read_lock_info(self.engine.lock_path)
            lock_pid = info.pid if info.pid and process_alive(info.pid) else None
            if not lock_pid:
                self.engine.lock_path.unlink(missing_ok=True)

        with state_lock(self.engine.state_path):
            state = load_state(self.engine.state_path)
            if lock_pid:
                if state.runner_pid != lock_pid or state.status != "running":
                    new_state = RunnerState(
                        last_run_id=state.last_run_id,
                        status="running",
                        last_exit_code=state.last_exit_code,
                        last_run_started_at=state.last_run_started_at or now_iso(),
                        last_run_finished_at=None,
                        autorunner_model_override=state.autorunner_model_override,
                        autorunner_effort_override=state.autorunner_effort_override,
                        autorunner_approval_policy=state.autorunner_approval_policy,
                        autorunner_sandbox_mode=state.autorunner_sandbox_mode,
                        autorunner_workspace_write_network=state.autorunner_workspace_write_network,
                        runner_pid=lock_pid,
                        sessions=state.sessions,
                        repo_to_session=state.repo_to_session,
                    )
                    save_state(self.engine.state_path, new_state)
                return

            pid = state.runner_pid
            if pid and not process_alive(pid):
                status = state.status
                exit_code = state.last_exit_code
                finished_at = state.last_run_finished_at
                if status == "running":
                    status = "error"
                    if exit_code is None:
                        exit_code = 1
                    if finished_at is None:
                        finished_at = now_iso()
                new_state = RunnerState(
                    last_run_id=state.last_run_id,
                    status=status,
                    last_exit_code=exit_code,
                    last_run_started_at=state.last_run_started_at,
                    last_run_finished_at=finished_at,
                    autorunner_model_override=state.autorunner_model_override,
                    autorunner_effort_override=state.autorunner_effort_override,
                    autorunner_approval_policy=state.autorunner_approval_policy,
                    autorunner_sandbox_mode=state.autorunner_sandbox_mode,
                    autorunner_workspace_write_network=state.autorunner_workspace_write_network,
                    runner_pid=None,
                    sessions=state.sessions,
                    repo_to_session=state.repo_to_session,
                )
                save_state(self.engine.state_path, new_state)

    def _ensure_unlocked(self) -> None:
        if not self.engine.lock_path.exists():
            return
        info = read_lock_info(self.engine.lock_path)
        pid = info.pid
        if pid and process_alive(pid):
            raise LockError(
                f"Another autorunner is active (pid={pid}); use --force to override"
            )
        self.engine.lock_path.unlink(missing_ok=True)

    def _spawn_runner(self, *, action: str, once: bool = False) -> None:
        cmd = build_runner_cmd(
            self.engine.repo_root,
            action=action,
            once=once,
        )
        self._spawn_fn(cmd, self.engine)

    def start(self, once: bool = False) -> None:
        with self._lock:
            self.reconcile()
            self._ensure_unlocked()
            self.engine.clear_stop_request()
            action = "once" if once else "run"
            self._spawn_runner(action=action)

    def resume(self, once: bool = False) -> None:
        with self._lock:
            self.reconcile()
            self._ensure_unlocked()
            self.engine.clear_stop_request()
            self._spawn_runner(action="resume", once=once)

    def stop(self) -> None:
        with self._lock:
            self.engine.request_stop()

    def kill(self) -> int | None:
        with self._lock:
            return self.engine.kill_running_process()
