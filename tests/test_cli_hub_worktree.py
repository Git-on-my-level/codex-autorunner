import json
from pathlib import Path
from typing import Optional

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.hub import (
    HubSupervisor,
    LockStatus,
    RepoSnapshot,
    RepoStatus,
)


def _snapshot(
    base_path: Path,
    repo_id: str,
    *,
    kind: str,
    worktree_of: Optional[str] = None,
    branch: Optional[str] = None,
) -> RepoSnapshot:
    return RepoSnapshot(
        id=repo_id,
        path=base_path / repo_id,
        display_name=repo_id,
        enabled=True,
        auto_run=False,
        worktree_setup_commands=None,
        kind=kind,
        worktree_of=worktree_of,
        branch=branch,
        exists_on_disk=True,
        is_clean=True,
        initialized=True,
        init_error=None,
        status=RepoStatus.IDLE,
        lock_status=LockStatus.UNLOCKED,
        last_run_id=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        last_exit_code=None,
        runner_pid=None,
    )


def test_cli_hub_worktree_list_filters_worktrees(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    base = _snapshot(tmp_path, "base", kind="base")
    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_list(self, *, use_cache: bool = True):
        return [base, worktree]

    monkeypatch.setattr(HubSupervisor, "list_repos", _fake_list)

    runner = CliRunner()
    result = runner.invoke(app, ["hub", "worktree", "list", "--path", str(hub_root)])
    assert result.exit_code == 0

    lines = [
        line for line in result.output.splitlines() if line.strip().startswith("-")
    ]
    assert len(lines) == 1
    assert "base--feature" in lines[0]


def test_cli_hub_worktree_scan_filters_worktrees_json(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    base = _snapshot(tmp_path, "base", kind="base")
    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_scan(self):
        return [base, worktree]

    monkeypatch.setattr(HubSupervisor, "scan", _fake_scan)

    runner = CliRunner()
    result = runner.invoke(
        app, ["hub", "worktree", "scan", "--path", str(hub_root), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [item["id"] for item in payload["worktrees"]] == ["base--feature"]


def test_cli_hub_worktree_create_prints_details(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_create(self, *, base_repo_id, branch, force=False, start_point=None):
        return worktree

    monkeypatch.setattr(HubSupervisor, "create_worktree", _fake_create)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "create",
            "base",
            "feature",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    assert "base--feature" in result.output
    assert "feature" in result.output
    assert str(worktree.path) in result.output


def test_cli_hub_worktree_cleanup_calls_supervisor(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    calls = {}

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
    ):
        calls["worktree_repo_id"] = worktree_repo_id
        calls["delete_branch"] = delete_branch
        calls["delete_remote"] = delete_remote
        calls["archive"] = archive
        calls["force_archive"] = force_archive
        calls["archive_note"] = archive_note

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "cleanup",
            "wt-1",
            "--path",
            str(hub_root),
            "--no-archive",
        ],
    )
    assert result.exit_code == 0
    assert calls["worktree_repo_id"] == "wt-1"
    assert calls["archive"] is False
