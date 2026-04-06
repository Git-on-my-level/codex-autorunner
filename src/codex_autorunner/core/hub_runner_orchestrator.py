from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

from ..manifest import load_manifest
from .config import HubConfig, RepoConfig, derive_repo_config
from .ports.backend_orchestrator import (
    BackendOrchestrator as BackendOrchestratorProtocol,
)
from .runner_controller import SpawnRunnerFn
from .state import load_state
from .types import AppServerSupervisorFactory, BackendFactory

if TYPE_CHECKING:
    from .hub import RepoRunner

logger = logging.getLogger("codex_autorunner.hub_runner_orchestrator")

BackendFactoryBuilder = Callable[[Path, RepoConfig], BackendFactory]
AppServerSupervisorFactoryBuilder = Callable[[RepoConfig], AppServerSupervisorFactory]
BackendOrchestratorBuilder = Callable[[Path, RepoConfig], BackendOrchestratorProtocol]


class RunnerOrchestrator:
    def __init__(
        self,
        hub_config: HubConfig,
        *,
        spawn_fn: Optional[SpawnRunnerFn] = None,
        backend_factory_builder: Optional[BackendFactoryBuilder] = None,
        app_server_supervisor_factory_builder: Optional[
            AppServerSupervisorFactoryBuilder
        ] = None,
        backend_orchestrator_builder: Optional[BackendOrchestratorBuilder] = None,
        agent_id_validator: Optional[Callable[[str], str]] = None,
    ):
        self.hub_config = hub_config
        self._runners: Dict[str, RepoRunner] = {}
        self._spawn_fn = spawn_fn
        self._backend_factory_builder = backend_factory_builder
        self._app_server_supervisor_factory_builder = (
            app_server_supervisor_factory_builder
        )
        self._backend_orchestrator_builder = backend_orchestrator_builder
        self._agent_id_validator = agent_id_validator

    @property
    def runners(self) -> Dict[str, RepoRunner]:
        return self._runners

    def ensure_runner(
        self, repo_id: str, allow_uninitialized: bool = False
    ) -> Optional[RepoRunner]:
        from .hub import RepoRunner

        if repo_id in self._runners:
            return self._runners[repo_id]
        manifest = load_manifest(self.hub_config.manifest_path, self.hub_config.root)
        repo = manifest.get(repo_id)
        if not repo:
            raise ValueError(f"Repo {repo_id} not found in manifest")
        repo_root = (self.hub_config.root / repo.path).resolve()
        tickets_dir = repo_root / ".codex-autorunner" / "tickets"
        if not allow_uninitialized and not tickets_dir.exists():
            raise ValueError(f"Repo {repo_id} is not initialized")
        if not tickets_dir.exists():
            return None
        repo_config = derive_repo_config(self.hub_config, repo_root, load_env=False)
        runner = RepoRunner(
            repo_id,
            repo_root,
            repo_config=repo_config,
            spawn_fn=self._spawn_fn,
            backend_factory_builder=self._backend_factory_builder,
            app_server_supervisor_factory_builder=(
                self._app_server_supervisor_factory_builder
            ),
            backend_orchestrator_builder=self._backend_orchestrator_builder,
            agent_id_validator=self._agent_id_validator,
        )
        self._runners[repo_id] = runner
        return runner

    def run(self, repo_id: str, once: bool = False) -> None:
        runner = self.ensure_runner(repo_id)
        assert runner is not None
        runner.start(once=once)

    def stop(self, repo_id: str) -> None:
        runner = self.ensure_runner(repo_id, allow_uninitialized=True)
        if runner:
            runner.stop()

    def stop_and_wait_for_exit(
        self,
        *,
        repo_id: str,
        repo_path: Path,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        from .hub import LockStatus, read_lock_status

        runner = self.ensure_runner(repo_id, allow_uninitialized=True)
        if not runner:
            return

        runner.stop()
        deadline = time.monotonic() + max(timeout_seconds, poll_interval_seconds)
        lock_path = repo_path / ".codex-autorunner" / "lock"
        state_path = repo_path / ".codex-autorunner" / "state.sqlite3"
        last_error: Optional[Exception] = None

        while True:
            runner.reconcile()

            try:
                lock_status = read_lock_status(lock_path)
                runner_state = load_state(state_path) if state_path.exists() else None
                last_error = None
            except (OSError, ValueError, sqlite3.Error) as exc:
                last_error = exc
            else:
                if lock_status != LockStatus.LOCKED_ALIVE and (
                    runner_state is None
                    or (
                        runner_state.runner_pid is None
                        and runner_state.status != "running"
                    )
                ):
                    return

            if time.monotonic() >= deadline:
                message = f"Timed out waiting for repo runner to stop before proceeding: {repo_id}"
                if last_error is not None:
                    raise ValueError(f"{message} ({last_error})") from last_error
                raise ValueError(message)

            time.sleep(poll_interval_seconds)

    def resume(self, repo_id: str, once: bool = False) -> None:
        runner = self.ensure_runner(repo_id)
        assert runner is not None
        runner.resume(once=once)

    def kill(self, repo_id: str) -> None:
        runner = self.ensure_runner(repo_id, allow_uninitialized=True)
        if runner:
            runner.kill()
