from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    HubConfig,
    load_hub_config,
)
from codex_autorunner.core.hub_runner_orchestrator import RunnerOrchestrator
from codex_autorunner.core.runner_controller import ProcessRunnerController
from codex_autorunner.manifest import Manifest, ManifestRepo, save_manifest
from tests.conftest import write_test_config


def _make_hub_config(tmp_path: Path) -> HubConfig:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    return load_hub_config(hub_root)


def _seed_manifest(hub_config: HubConfig, repo_id: str = "demo") -> Path:
    manifest = Manifest(
        version=2,
        repos=[ManifestRepo(id=repo_id, path=Path(repo_id))],
        agent_workspaces=[],
    )
    save_manifest(hub_config.manifest_path, manifest, hub_config.root)
    repo_root = (hub_config.root / repo_id).resolve()
    return repo_root


def _seed_initialized_repo(hub_config: HubConfig, repo_id: str = "demo") -> Path:
    repo_root = _seed_manifest(hub_config, repo_id)
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    return repo_root


def _build_backend_orchestrator(repo_root: Path, repo_config):
    from codex_autorunner.core.orchestration.sqlite import (
        open_orchestration_sqlite,
    )

    db = open_orchestration_sqlite(repo_root)
    return db


class TestEnsureRunner:
    def test_raises_if_repo_not_in_manifest(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        with pytest.raises(ValueError, match="not found in manifest"):
            orchestrator.ensure_runner("nonexistent")

    def test_raises_if_not_initialized(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_manifest(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        with pytest.raises(ValueError, match="not initialized"):
            orchestrator.ensure_runner("demo")

    def test_returns_none_if_uninitialized_and_allowed(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_manifest(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        result = orchestrator.ensure_runner("demo", allow_uninitialized=True)
        assert result is None

    def test_creates_and_caches_runner(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_initialized_repo(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        runner = orchestrator.ensure_runner("demo")
        assert runner is not None
        assert "demo" in orchestrator.runners
        same = orchestrator.ensure_runner("demo")
        assert same is runner


class TestRun:
    def test_run_calls_start(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_initialized_repo(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )

        start_calls: list[tuple[bool]] = []

        def fake_start(self, once: bool = False) -> None:
            start_calls.append((once,))

        with patch.object(ProcessRunnerController, "start", fake_start):
            orchestrator.run("demo", once=True)

        assert start_calls == [(True,)]


class TestStop:
    def test_stop_calls_runner_stop(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_initialized_repo(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )

        stop_calls: list[None] = []

        def fake_stop(self) -> None:
            stop_calls.append(None)

        with patch.object(ProcessRunnerController, "stop", fake_stop):
            orchestrator.stop("demo")

        assert len(stop_calls) == 1

    def test_stop_noop_if_uninitialized(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_manifest(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        orchestrator.stop("demo")


class TestResume:
    def test_resume_calls_runner_resume(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_initialized_repo(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )

        resume_calls: list[tuple[bool]] = []

        def fake_resume(self, once: bool = False) -> None:
            resume_calls.append((once,))

        with patch.object(ProcessRunnerController, "resume", fake_resume):
            orchestrator.resume("demo", once=True)

        assert resume_calls == [(True,)]


class TestKill:
    def test_kill_calls_runner_kill(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_initialized_repo(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )

        kill_calls: list[None] = []

        def fake_kill(self) -> Optional[int]:
            kill_calls.append(None)
            return 0

        with patch.object(ProcessRunnerController, "kill", fake_kill):
            orchestrator.kill("demo")

        assert len(kill_calls) == 1

    def test_kill_noop_if_uninitialized(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_manifest(hub_config, "demo")
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        orchestrator.kill("demo")


class TestStopAndWaitForExit:
    def test_returns_immediately_if_no_runner(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        _seed_manifest(hub_config, "demo")
        repo_root = (hub_config.root / "demo").resolve()
        repo_root.mkdir(parents=True, exist_ok=True)
        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )
        orchestrator.stop_and_wait_for_exit(
            repo_id="demo",
            repo_path=repo_root,
            timeout_seconds=0.1,
        )

    def test_times_out_if_runner_never_stops(self, tmp_path: Path) -> None:
        hub_config = _make_hub_config(tmp_path)
        repo_root = _seed_initialized_repo(hub_config, "demo")

        lock_path = repo_root / ".codex-autorunner" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({"pid": 999999999, "started_at": "2025-01-01"}), "utf-8"
        )

        state_path = repo_root / ".codex-autorunner" / "state.sqlite3"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}", "utf-8")

        orchestrator = RunnerOrchestrator(
            hub_config,
            backend_orchestrator_builder=_build_backend_orchestrator,
        )

        def fake_reconcile(self) -> None:
            pass

        import codex_autorunner.core.hub_topology as hub_topology_module

        with patch.object(ProcessRunnerController, "reconcile", fake_reconcile):
            with patch.object(
                hub_topology_module,
                "read_lock_status",
                return_value=hub_topology_module.LockStatus.LOCKED_ALIVE,
            ):
                with pytest.raises(ValueError, match="Timed out"):
                    orchestrator.stop_and_wait_for_exit(
                        repo_id="demo",
                        repo_path=repo_root,
                        timeout_seconds=0.05,
                        poll_interval_seconds=0.01,
                    )
