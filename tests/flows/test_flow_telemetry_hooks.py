from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autorunner.core.config import FlowRetentionConfig
from codex_autorunner.core.flows.flow_telemetry_hooks import (
    SweepResult,
    housekeep_on_run_terminal,
    housekeep_on_worktree_cleanup,
    housekeep_sweep_repos,
)
from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_repo(temp_dir: Path, name: str = "repo") -> Path:
    repo_root = temp_dir / name
    codex_dir = repo_root / ".codex-autorunner"
    codex_dir.mkdir(parents=True, exist_ok=True)
    return repo_root


def _make_store(repo_root: Path) -> FlowStore:
    codex_dir = repo_root / ".codex-autorunner"
    codex_dir.mkdir(parents=True, exist_ok=True)
    db_path = codex_dir / "flows.db"
    store = FlowStore(db_path)
    store.initialize()
    return store


def _create_expired_run(store: FlowStore, run_id: str = "run-old") -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(
        record.id,
        status=FlowRunStatus.COMPLETED,
        finished_at="2020-01-01T00:00:00Z",
    )
    return record.id


def _create_recent_terminal_run(store: FlowStore, run_id: str = "run-recent") -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(
        record.id, status=FlowRunStatus.COMPLETED, finished_at="2099-01-01T00:00:00Z"
    )
    return record.id


def _create_active_run(store: FlowStore, run_id: str = "run-active") -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(
        record.id, status=FlowRunStatus.RUNNING, started_at="2025-01-01T00:00:00Z"
    )
    return record.id


def _add_event(
    store: FlowStore,
    run_id: str,
    event_type: FlowEventType,
    data: dict,
    event_id: str | None = None,
) -> None:
    store.create_event(
        event_id=event_id or f"evt-{event_type.value}-{run_id}",
        run_id=run_id,
        event_type=event_type,
        data=data,
    )


@pytest.fixture
def mock_retention_config():
    with patch(
        "codex_autorunner.core.flows.flow_telemetry_hooks._resolve_retention_config",
        return_value=FlowRetentionConfig(),
    ):
        yield


@pytest.fixture
def mock_open_store():
    """Patch _open_store to avoid needing a full repo config."""

    def _setup(repo_root: Path):
        store = _make_store(repo_root)
        original = store.initialize
        return store, original

    return _setup


class TestHousekeepOnRunTerminal:
    def test_no_db(self, temp_dir, mock_retention_config):
        repo_root = _make_repo(temp_dir, "repo-no-db")
        result = housekeep_on_run_terminal(repo_root, "run-1")
        assert result is None

    def test_with_expired_run(self, temp_dir, mock_retention_config):
        repo_root = _make_repo(temp_dir, "repo-expired")
        store = _make_store(repo_root)
        run_id = _create_expired_run(store, "run-old")
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {"message": {"method": "test", "params": {}}, "turn_id": "t1"},
            event_id="evt-1",
        )
        store.close()

        with patch(
            "codex_autorunner.core.flows.flow_telemetry_hooks._open_store",
            return_value=store,
        ):
            result = housekeep_on_run_terminal(repo_root, run_id)

        assert result is not None
        assert result.runs_processed == 1
        assert result.events_pruned == 1

    def test_with_active_run_skipped(self, temp_dir, mock_retention_config):
        repo_root = _make_repo(temp_dir, "repo-active")
        store = _make_store(repo_root)
        run_id = _create_active_run(store, "run-active")
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {"message": {"method": "test", "params": {}}, "turn_id": "t1"},
            event_id="evt-active-1",
        )
        store.close()

        with patch(
            "codex_autorunner.core.flows.flow_telemetry_hooks._open_store",
            return_value=store,
        ):
            result = housekeep_on_run_terminal(repo_root, run_id)

        assert result is not None
        assert result.runs_processed == 0


class TestHousekeepOnWorktreeCleanup:
    def test_no_db(self, temp_dir, mock_retention_config):
        repo_root = _make_repo(temp_dir, "wt-no-db")
        result = housekeep_on_worktree_cleanup(repo_root)
        assert result is None

    def test_prunes_expired(self, temp_dir, mock_retention_config):
        repo_root = _make_repo(temp_dir, "wt-expired")
        store = _make_store(repo_root)
        run_id = _create_expired_run(store, "run-old")
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {"message": {"method": "test", "params": {}}, "turn_id": "t1"},
            event_id="evt-1",
        )
        store.close()

        with patch(
            "codex_autorunner.core.flows.flow_telemetry_hooks._open_store",
            return_value=store,
        ):
            result = housekeep_on_worktree_cleanup(repo_root)

        assert result is not None
        assert result.runs_processed == 1

    def test_prunes_recent_terminal_runs_on_cleanup(
        self, temp_dir, mock_retention_config
    ):
        repo_root = _make_repo(temp_dir, "wt-recent")
        store = _make_store(repo_root)
        run_id = _create_recent_terminal_run(store, "run-recent")
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {"message": {"method": "test", "params": {}}, "turn_id": "t1"},
            event_id="evt-recent-1",
        )
        store.close()

        with patch(
            "codex_autorunner.core.flows.flow_telemetry_hooks._open_store",
            return_value=store,
        ):
            result = housekeep_on_worktree_cleanup(repo_root)

        assert result is not None
        assert result.runs_processed == 1


class TestHousekeepSweepRepos:
    def test_empty(self, temp_dir, mock_retention_config):
        result = housekeep_sweep_repos([])
        assert result.repos_scanned == 0
        assert result.repos_pruned == 0

    def test_no_db_dirs(self, temp_dir, mock_retention_config):
        roots = [_make_repo(temp_dir, "repo-a"), _make_repo(temp_dir, "repo-b")]
        result = housekeep_sweep_repos(roots)
        assert result.repos_scanned == 0

    def test_mixed_repos(self, temp_dir, mock_retention_config):
        repo_with_data = _make_repo(temp_dir, "repo-data")
        store = _make_store(repo_with_data)
        run_id = _create_expired_run(store, "run-old")
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {"message": {"method": "test", "params": {}}, "turn_id": "t1"},
            event_id="evt-1",
        )
        store.close()

        repo_empty = _make_repo(temp_dir, "repo-empty")

        with patch(
            "codex_autorunner.core.flows.flow_telemetry_hooks._open_store",
            side_effect=[
                store,
                None,
            ],
        ):
            result = housekeep_sweep_repos([repo_with_data, repo_empty])

        assert result.repos_scanned >= 1
        assert result.repos_pruned >= 1
        assert result.runs_processed >= 1

    def test_error_handling(self, temp_dir, mock_retention_config):
        repo_root = _make_repo(temp_dir, "repo-err")
        codex_dir = repo_root / ".codex-autorunner"
        codex_dir.mkdir(parents=True, exist_ok=True)
        db_path = codex_dir / "flows.db"
        db_path.write_text("not a real db")

        result = housekeep_sweep_repos([repo_root])
        assert result.errors == 1


class TestSweepResult:
    def test_defaults(self):
        result = SweepResult()
        assert result.repos_scanned == 0
        assert result.repos_pruned == 0
        assert result.runs_processed == 0
        assert result.events_exported == 0
        assert result.events_pruned == 0
        assert result.errors == 0
