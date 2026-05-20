import threading
from typing import Callable, Optional

from .locks import DEFAULT_RUNNER_CMD_HINTS, assess_lock, process_matches_identity
from .runner_process import build_runner_cmd, spawn_detached
from .runtime import LockError, RuntimeContext
from .state import (
    load_state,
    runner_observed_lock_start,
    runner_stale_running_to_error,
    save_state,
    state_lock,
)

SpawnRunnerFn = Callable[[list[str], RuntimeContext], object]


def _spawn_detached(cmd: list[str], ctx: RuntimeContext) -> object:
    return spawn_detached(cmd, cwd=ctx.repo_root)


class ProcessRunnerController:
    def __init__(
        self, ctx: RuntimeContext, *, spawn_fn: Optional[SpawnRunnerFn] = None
    ):
        self.ctx = ctx
        self._spawn_fn = spawn_fn or _spawn_detached
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.ctx.runner_pid() is not None

    def reconcile(self) -> None:
        lock_pid = None
        if self.ctx.lock_path.exists():
            assessment = assess_lock(
                self.ctx.lock_path,
                expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
            )
            if assessment.freeable:
                self.ctx.lock_path.unlink(missing_ok=True)
            else:
                lock_pid = assessment.pid

        durable = self.ctx.config.durable_writes
        with state_lock(self.ctx.state_path):
            state = load_state(self.ctx.state_path, durable=durable)
            if lock_pid:
                if state.runner_pid != lock_pid or state.status != "running":
                    new_state = runner_observed_lock_start(state, runner_pid=lock_pid)
                    save_state(self.ctx.state_path, new_state, durable=durable)
                return

            pid = state.runner_pid
            if pid and not process_matches_identity(
                pid,
                expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
            ):
                new_state = runner_stale_running_to_error(state)
                save_state(self.ctx.state_path, new_state, durable=durable)

    def _ensure_unlocked(self) -> None:
        if not self.ctx.lock_path.exists():
            return
        assessment = self._clear_freeable_lock()
        if assessment.freeable:
            return
        pid = assessment.pid
        if pid:
            raise LockError(
                f"Another autorunner is active (pid={pid}); use --force to override"
            )
        raise LockError("Another autorunner is active; stop it before continuing")

    def _clear_freeable_lock(self):
        assessment = assess_lock(
            self.ctx.lock_path,
            expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
        )
        if not assessment.freeable:
            return assessment
        self.ctx.lock_path.unlink(missing_ok=True)
        durable = self.ctx.config.durable_writes
        with state_lock(self.ctx.state_path):
            state = load_state(self.ctx.state_path, durable=durable)
            if state.status == "running" or state.runner_pid:
                new_state = runner_stale_running_to_error(state)
                save_state(self.ctx.state_path, new_state, durable=durable)
        return assessment

    def clear_freeable_lock(self):
        with self._lock:
            return self._clear_freeable_lock()

    def _spawn_runner(self, *, action: str, once: bool = False) -> None:
        cmd = build_runner_cmd(
            self.ctx.repo_root,
            action=action,
            once=once,
        )
        self._spawn_fn(cmd, self.ctx)

    def start(self, once: bool = False) -> None:
        with self._lock:
            self.reconcile()
            self._ensure_unlocked()
            self.ctx.clear_stop_request()
            action = "once" if once else "run"
            self._spawn_runner(action=action)

    def resume(self, once: bool = False) -> None:
        with self._lock:
            self.reconcile()
            self._ensure_unlocked()
            self.ctx.clear_stop_request()
            self._spawn_runner(action="resume", once=once)

    def stop(self) -> None:
        with self._lock:
            self.ctx.request_stop()

    def kill(self) -> Optional[int]:
        with self._lock:
            return self.ctx.kill_running_process()
