"""Tests for stale flow/run cleanup features (issue #652)."""

from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.pma_context import _gather_inbox
from codex_autorunner.surfaces.cli.cli import _stale_terminal_runs


def _create_flow_run(repo_root: Path, run_id: str, status: FlowRunStatus) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, status)


def _write_dispatch_history(
    repo_root: Path, run_id: str, seq: int, *, mode: str = "pause"
) -> None:
    dispatch_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / f"{seq:04d}"
    )
    dispatch_dir.mkdir(parents=True)
    (dispatch_dir / "DISPATCH.md").write_text(
        f"---\nmode: {mode}\ntitle: dispatch-{seq}\n---\n\nPlease review.\n",
        encoding="utf-8",
    )


def test_stale_terminal_runs_filters_correctly():
    """Test that _stale_terminal_runs only returns FAILED/STOPPED runs."""
    from codex_autorunner.core.flows.models import FlowRunRecord

    runs = [
        FlowRunRecord(
            id="run-1",
            flow_type="ticket_flow",
            status=FlowRunStatus.RUNNING,
            current_step="step",
            created_at="2024-01-01T00:00:00Z",
            input_data={},
        ),
        FlowRunRecord(
            id="run-2",
            flow_type="ticket_flow",
            status=FlowRunStatus.PAUSED,
            current_step="step",
            created_at="2024-01-01T00:00:00Z",
            input_data={},
        ),
        FlowRunRecord(
            id="run-3",
            flow_type="ticket_flow",
            status=FlowRunStatus.FAILED,
            current_step="step",
            created_at="2024-01-01T00:00:00Z",
            input_data={},
        ),
        FlowRunRecord(
            id="run-4",
            flow_type="ticket_flow",
            status=FlowRunStatus.STOPPED,
            current_step="step",
            created_at="2024-01-01T00:00:00Z",
            input_data={},
        ),
        FlowRunRecord(
            id="run-5",
            flow_type="ticket_flow",
            status=FlowRunStatus.COMPLETED,
            current_step="step",
            created_at="2024-01-01T00:00:00Z",
            input_data={},
        ),
    ]

    stale = _stale_terminal_runs(runs)

    assert len(stale) == 2
    assert {r.id for r in stale} == {"run-3", "run-4"}


def test_gather_inbox_hides_stale_when_active_run_exists(tmp_path: Path) -> None:
    """Test that stale runs are hidden from inbox when an active sibling exists."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True)

    _create_flow_run(repo_root, "active-run", FlowRunStatus.RUNNING)
    _create_flow_run(repo_root, "stale-run-1", FlowRunStatus.FAILED)
    _create_flow_run(repo_root, "stale-run-2", FlowRunStatus.STOPPED)

    from unittest.mock import MagicMock

    from codex_autorunner.core.hub import RepoSnapshot

    mock_supervisor = MagicMock()
    mock_supervisor.list_repos.return_value = [
        RepoSnapshot(
            id="test-repo",
            path=repo_root,
            display_name="Test Repo",
            enabled=True,
            auto_run=False,
            worktree_setup_commands=None,
            kind="base",
            worktree_of=None,
            branch="main",
            exists_on_disk=True,
            is_clean=True,
            initialized=True,
            init_error=None,
            status="running",
            lock_status="unlocked",
            last_run_id="active-run",
            last_run_started_at=None,
            last_run_finished_at=None,
            last_exit_code=None,
            runner_pid=None,
        )
    ]

    messages = _gather_inbox(mock_supervisor, max_text_chars=1000)

    run_ids = {m.get("run_id") for m in messages}

    assert (
        "stale-run-1" not in run_ids
    ), "FAILED run should be hidden when active sibling exists"
    assert (
        "stale-run-2" not in run_ids
    ), "STOPPED run should be hidden when active sibling exists"


def test_gather_inbox_shows_stale_when_no_active_run(tmp_path: Path) -> None:
    """Test that stale runs are shown when no active sibling exists."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True)

    _create_flow_run(repo_root, "stale-run", FlowRunStatus.FAILED)

    from unittest.mock import MagicMock

    from codex_autorunner.core.hub import RepoSnapshot

    mock_supervisor = MagicMock()
    mock_supervisor.list_repos.return_value = [
        RepoSnapshot(
            id="test-repo",
            path=repo_root,
            display_name="Test Repo",
            enabled=True,
            auto_run=False,
            worktree_setup_commands=None,
            kind="base",
            worktree_of=None,
            branch="main",
            exists_on_disk=True,
            is_clean=True,
            initialized=True,
            init_error=None,
            status="idle",
            lock_status="unlocked",
            last_run_id="stale-run",
            last_run_started_at=None,
            last_run_finished_at=None,
            last_exit_code=None,
            runner_pid=None,
        )
    ]

    messages = _gather_inbox(mock_supervisor, max_text_chars=1000)

    run_ids = {m.get("run_id") for m in messages}

    assert "stale-run" in run_ids


def test_gather_inbox_hides_paused_when_newer_run_is_active(tmp_path: Path) -> None:
    """Test that older paused runs are hidden when a newer active run exists."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True)

    _create_flow_run(repo_root, "active-run", FlowRunStatus.RUNNING)
    _create_flow_run(repo_root, "paused-run", FlowRunStatus.PAUSED)

    _write_dispatch_history(repo_root, "paused-run", seq=1, mode="pause")

    from unittest.mock import MagicMock

    from codex_autorunner.core.hub import RepoSnapshot

    mock_supervisor = MagicMock()
    mock_supervisor.list_repos.return_value = [
        RepoSnapshot(
            id="test-repo",
            path=repo_root,
            display_name="Test Repo",
            enabled=True,
            auto_run=False,
            worktree_setup_commands=None,
            kind="base",
            worktree_of=None,
            branch="main",
            exists_on_disk=True,
            is_clean=True,
            initialized=True,
            init_error=None,
            status="running",
            lock_status="unlocked",
            last_run_id="active-run",
            last_run_started_at=None,
            last_run_finished_at=None,
            last_exit_code=None,
            runner_pid=None,
        )
    ]

    messages = _gather_inbox(mock_supervisor, max_text_chars=1000)

    run_ids = {m.get("run_id") for m in messages}

    assert "paused-run" not in run_ids


def test_gather_inbox_hides_older_paused_when_newer_completed_exists(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".codex-autorunner" / "tickets").mkdir(parents=True)

    _create_flow_run(repo_root, "older-paused", FlowRunStatus.PAUSED)
    _write_dispatch_history(repo_root, "older-paused", seq=1, mode="pause")
    _create_flow_run(repo_root, "newer-completed", FlowRunStatus.COMPLETED)

    from unittest.mock import MagicMock

    from codex_autorunner.core.hub import RepoSnapshot

    mock_supervisor = MagicMock()
    mock_supervisor.list_repos.return_value = [
        RepoSnapshot(
            id="test-repo",
            path=repo_root,
            display_name="Test Repo",
            enabled=True,
            auto_run=False,
            worktree_setup_commands=None,
            kind="base",
            worktree_of=None,
            branch="main",
            exists_on_disk=True,
            is_clean=True,
            initialized=True,
            init_error=None,
            status="idle",
            lock_status="unlocked",
            last_run_id="newer-completed",
            last_run_started_at=None,
            last_run_finished_at=None,
            last_exit_code=None,
            runner_pid=None,
        )
    ]

    messages = _gather_inbox(mock_supervisor, max_text_chars=1000)
    assert messages == []
