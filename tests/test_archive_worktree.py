import os
import shutil
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core import archive as archive_module
from codex_autorunner.core.archive import (
    archive_workspace_car_state,
    archive_worktree_snapshot,
    has_car_state,
)
from codex_autorunner.core.archive_retention import WorktreeArchiveRetentionPolicy


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_worktree(tmp_path: Path) -> tuple[Path, Path]:
    base_repo = tmp_path / "base"
    worktree_repo = tmp_path / "worktree"
    base_repo.mkdir()
    worktree_repo.mkdir()

    car_root = worktree_repo / ".codex-autorunner"
    (car_root / "contextspace").mkdir(parents=True)
    (car_root / "tickets").mkdir(parents=True)
    (car_root / "runs" / "run-1" / "dispatch").mkdir(parents=True)
    (car_root / "flows").mkdir(parents=True)

    _write(car_root / "contextspace" / "active_context.md", "hello")
    _write(car_root / "tickets" / "TICKET-001.md", "ticket")
    _write(car_root / "runs" / "run-1" / "dispatch" / "DISPATCH.md", "dispatch")
    _write(car_root / "flows.db", "flows-db")
    _write(car_root / "config.yml", "version: 2\n")
    _write(car_root / "state.sqlite3", "state")
    _write(car_root / "app_server_threads.json", '{"threads": 1}\n')
    _write(
        car_root / "app_server_workspaces" / "workspace-1" / "meta.json",
        '{"workspace": "demo"}\n',
    )
    _write(car_root / "filebox" / "outbox" / "reply.txt", "artifact")
    _write(car_root / "codex-autorunner.log", "log-a")
    _write(car_root / "codex-server.log", "log-b")
    _write(car_root / "github_context" / "issue.md", "issue context")

    run_one = car_root / "flows" / "11111111-1111-1111-1111-111111111111"
    run_two = car_root / "flows" / "22222222-2222-2222-2222-222222222222"
    run_one.mkdir(parents=True)
    run_two.mkdir(parents=True)
    _write(run_one / "meta.json", "one")
    _write(run_two / "meta.json", "two")
    os.utime(run_one, (100000, 100000))
    os.utime(run_two, (200000, 200000))

    return base_repo, worktree_repo


def test_archive_snapshot_copies_curated_paths(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert result.snapshot_path.exists()
    assert (result.snapshot_path / "contextspace" / "active_context.md").read_text(
        encoding="utf-8"
    ) == "hello"
    assert (result.snapshot_path / "tickets" / "TICKET-001.md").exists()
    assert (
        result.snapshot_path / "runs" / "run-1" / "dispatch" / "DISPATCH.md"
    ).exists()
    assert (result.snapshot_path / "config" / "config.yml").exists()
    assert (result.snapshot_path / "github_context" / "issue.md").exists()
    assert not (result.snapshot_path / "flows.db").exists()
    assert not (result.snapshot_path / "state" / "state.sqlite3").exists()
    assert not (result.snapshot_path / "logs" / "codex-autorunner.log").exists()
    assert not (result.snapshot_path / "logs" / "codex-server.log").exists()

    meta = result.meta_path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in meta
    assert '"status": "complete"' in meta


def test_cleanup_archive_intent_preserves_flow_store(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)
    _write(worktree_repo / ".codex-autorunner" / "flows.db-wal", "wal-data")
    _write(worktree_repo / ".codex-autorunner" / "flows.db-shm", "shm-data")

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
        intent="cleanup_snapshot",
    )

    assert (result.snapshot_path / "flows.db").exists()
    assert (result.snapshot_path / "flows.db-wal").read_text(encoding="utf-8") == (
        "wal-data"
    )
    assert (result.snapshot_path / "flows.db-shm").read_text(encoding="utf-8") == (
        "shm-data"
    )
    assert not (result.snapshot_path / "state" / "state.sqlite3").exists()
    assert not (result.snapshot_path / "logs" / "codex-autorunner.log").exists()


def test_archive_snapshot_skips_symlink_escape(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)
    car_root = worktree_repo / ".codex-autorunner"

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    escape = car_root / "contextspace" / "escape.txt"
    escape.symlink_to(secret)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert not (result.snapshot_path / "contextspace" / "escape.txt").exists()


def test_archive_summary_counts_files_and_flows(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert result.flow_run_count == 2
    assert result.latest_flow_run_id == "22222222-2222-2222-2222-222222222222"

    expected_files = 7
    assert result.file_count == expected_files

    total_bytes = 0
    for path in result.snapshot_path.rglob("*"):
        if path.is_file() and path.name != "META.json":
            total_bytes += path.stat().st_size
    assert result.total_bytes == total_bytes


def test_archive_snapshot_full_profile_keeps_runtime_state(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    result = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
        profile="full",
    )

    assert (result.snapshot_path / "flows.db").exists()
    assert (result.snapshot_path / "state" / "state.sqlite3").exists()
    assert (result.snapshot_path / "logs" / "codex-autorunner.log").exists()
    assert (result.snapshot_path / "logs" / "codex-server.log").exists()


def test_archive_snapshot_prunes_older_worktree_snapshots(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)
    policy = WorktreeArchiveRetentionPolicy(
        max_snapshots_per_repo=1,
        max_age_days=365,
        max_total_bytes=1_000_000,
    )

    first = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
        snapshot_id="20260101T000000Z--feature-one--1111111",
        head_sha="1111111",
        retention_policy=policy,
    )
    second = archive_worktree_snapshot(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
        snapshot_id="20260102T000000Z--feature-two--2222222",
        head_sha="2222222",
        retention_policy=policy,
    )

    assert not first.snapshot_path.exists()
    assert second.snapshot_path.exists()


def test_has_car_state_ignores_seeded_defaults(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_repo_files(repo_root, git_required=False)

    assert has_car_state(repo_root) is False

    _write(
        repo_root / ".codex-autorunner" / "tickets" / "TICKET-001-demo.md",
        "demo ticket",
    )
    assert has_car_state(repo_root) is True


def test_archive_workspace_car_state_resets_runtime_state(tmp_path: Path) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)
    _write(worktree_repo / ".codex-autorunner" / "flows.db-wal", "wal-data")
    _write(worktree_repo / ".codex-autorunner" / "flows.db-shm", "shm-data")

    result = archive_workspace_car_state(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert "tickets" in result.archived_paths
    assert "contextspace" in result.archived_paths
    assert "runs" in result.archived_paths
    assert "flows" in result.archived_paths
    assert "flows.db" in result.archived_paths
    assert "state.sqlite3" in result.archived_paths
    assert "app_server_threads.json" in result.archived_paths
    assert "app_server_workspaces" in result.archived_paths
    assert "filebox" in result.archived_paths
    assert "codex-autorunner.log" in result.archived_paths
    assert "codex-server.log" in result.archived_paths
    assert "config.yml" in result.archived_paths
    assert (result.snapshot_path / "tickets" / "TICKET-001.md").exists()
    assert (
        result.snapshot_path / "runs" / "run-1" / "dispatch" / "DISPATCH.md"
    ).exists()
    assert (result.snapshot_path / "contextspace" / "active_context.md").read_text(
        encoding="utf-8"
    ) == "hello"
    assert (result.snapshot_path / "flows.db").exists()
    assert (result.snapshot_path / "flows.db-wal").read_text(encoding="utf-8") == (
        "wal-data"
    )
    assert (result.snapshot_path / "flows.db-shm").read_text(encoding="utf-8") == (
        "shm-data"
    )
    assert (result.snapshot_path / "state" / "state.sqlite3").exists()
    assert (result.snapshot_path / "state" / "app_server_threads.json").exists()
    assert (
        result.snapshot_path / "app_server_workspaces" / "workspace-1" / "meta.json"
    ).exists()
    assert (result.snapshot_path / "filebox" / "outbox" / "reply.txt").exists()
    assert (result.snapshot_path / "logs" / "codex-autorunner.log").exists()
    assert (result.snapshot_path / "logs" / "codex-server.log").exists()

    tickets_dir = worktree_repo / ".codex-autorunner" / "tickets"
    assert (tickets_dir / "AGENTS.md").exists()
    assert not (tickets_dir / "TICKET-001.md").exists()
    assert (
        worktree_repo / ".codex-autorunner" / "contextspace" / "active_context.md"
    ).read_text(encoding="utf-8") == ""
    assert not (worktree_repo / ".codex-autorunner" / "runs").exists()
    assert not (worktree_repo / ".codex-autorunner" / "flows").exists()
    assert not (worktree_repo / ".codex-autorunner" / "filebox").exists()
    assert has_car_state(worktree_repo) is False


def test_archive_workspace_car_state_preserves_runtime_state_being_reset(
    tmp_path: Path,
) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    result = archive_workspace_car_state(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert "flows.db" in result.archived_paths
    assert "state.sqlite3" in result.archived_paths
    assert "filebox" in result.archived_paths
    assert "codex-autorunner.log" in result.archived_paths
    assert (result.snapshot_path / "flows.db").exists()
    assert (result.snapshot_path / "state" / "state.sqlite3").exists()
    assert (result.snapshot_path / "filebox" / "outbox" / "reply.txt").exists()


def test_archive_snapshot_stages_transactionally_until_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    def _fail_finalize(staging_root: Path, final_snapshot_root: Path) -> None:
        _ = staging_root, final_snapshot_root
        raise RuntimeError("finalize failed")

    monkeypatch.setattr(archive_module, "_finalize_snapshot_root", _fail_finalize)

    with pytest.raises(RuntimeError, match="finalize failed"):
        archive_worktree_snapshot(
            base_repo_root=base_repo,
            base_repo_id="base",
            worktree_repo_root=worktree_repo,
            worktree_repo_id="worktree",
            branch="feature/archive-viewer",
            worktree_of="base",
            snapshot_id="20260103T000000Z--feature-three--3333333",
            head_sha="3333333",
        )

    worktree_archive_root = (
        base_repo / ".codex-autorunner" / "archive" / "worktrees" / "worktree"
    )
    assert not (
        worktree_archive_root / "20260103T000000Z--feature-three--3333333"
    ).exists()
    assert list(worktree_archive_root.glob(".*.tmp-*")) == []


def test_archive_workspace_car_state_preserves_live_state_if_finalize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)

    def _fail_finalize(staging_root: Path, final_snapshot_root: Path) -> None:
        _ = staging_root, final_snapshot_root
        raise RuntimeError("finalize failed")

    monkeypatch.setattr(archive_module, "_finalize_snapshot_root", _fail_finalize)

    with pytest.raises(RuntimeError, match="finalize failed"):
        archive_workspace_car_state(
            base_repo_root=base_repo,
            base_repo_id="base",
            worktree_repo_root=worktree_repo,
            worktree_repo_id="worktree",
            branch="feature/archive-viewer",
            worktree_of="base",
            snapshot_id="20260104T000000Z--feature-four--4444444",
            head_sha="4444444",
        )

    worktree_archive_root = (
        base_repo / ".codex-autorunner" / "archive" / "worktrees" / "worktree"
    )
    assert not (
        worktree_archive_root / "20260104T000000Z--feature-four--4444444"
    ).exists()
    assert (worktree_repo / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert (worktree_repo / ".codex-autorunner" / "flows.db").exists()
    assert (worktree_repo / ".codex-autorunner" / "runs" / "run-1").exists()
    assert list(worktree_archive_root.glob(".*.tmp-*")) == []


def test_archive_workspace_car_state_preserves_legacy_workspace_context(
    tmp_path: Path,
) -> None:
    base_repo, worktree_repo = _setup_worktree(tmp_path)
    car_root = worktree_repo / ".codex-autorunner"
    shutil.rmtree(car_root / "contextspace")
    _write(car_root / "workspace" / "active_context.md", "legacy context")

    result = archive_workspace_car_state(
        base_repo_root=base_repo,
        base_repo_id="base",
        worktree_repo_root=worktree_repo,
        worktree_repo_id="worktree",
        branch="feature/archive-viewer",
        worktree_of="base",
    )

    assert (result.snapshot_path / "contextspace" / "active_context.md").read_text(
        encoding="utf-8"
    ) == "legacy context"


class TestContextspaceWorkspaceFallback:
    def test_prefers_contextspace_when_both_exist(self, tmp_path: Path) -> None:
        base_repo, worktree_repo = _setup_worktree(tmp_path)
        car_root = worktree_repo / ".codex-autorunner"
        _write(car_root / "contextspace" / "active_context.md", "new context")
        _write(car_root / "workspace" / "active_context.md", "old context")

        result = archive_workspace_car_state(
            base_repo_root=base_repo,
            base_repo_id="base",
            worktree_repo_root=worktree_repo,
            worktree_repo_id="worktree",
            branch="feature/archive-viewer",
            worktree_of="base",
        )

        assert (result.snapshot_path / "contextspace" / "active_context.md").read_text(
            encoding="utf-8"
        ) == "new context"

    def test_falls_back_to_workspace_when_contextspace_absent(
        self, tmp_path: Path
    ) -> None:
        base_repo, worktree_repo = _setup_worktree(tmp_path)
        car_root = worktree_repo / ".codex-autorunner"
        shutil.rmtree(car_root / "contextspace")
        _write(car_root / "workspace" / "active_context.md", "legacy context")
        _write(car_root / "workspace" / "decisions.md", "legacy decisions")

        result = archive_workspace_car_state(
            base_repo_root=base_repo,
            base_repo_id="base",
            worktree_repo_root=worktree_repo,
            worktree_repo_id="worktree",
            branch="feature/archive-viewer",
            worktree_of="base",
        )

        assert (result.snapshot_path / "contextspace" / "active_context.md").read_text(
            encoding="utf-8"
        ) == "legacy context"
        assert (result.snapshot_path / "contextspace" / "decisions.md").read_text(
            encoding="utf-8"
        ) == "legacy decisions"

    def test_workspace_reset_clears_both_contextspace_and_workspace(
        self, tmp_path: Path
    ) -> None:
        base_repo, worktree_repo = _setup_worktree(tmp_path)
        car_root = worktree_repo / ".codex-autorunner"
        _write(car_root / "workspace" / "active_context.md", "legacy")

        archive_workspace_car_state(
            base_repo_root=base_repo,
            base_repo_id="base",
            worktree_repo_root=worktree_repo,
            worktree_repo_id="worktree",
            branch="feature/archive-viewer",
            worktree_of="base",
        )

        assert not (car_root / "workspace").exists()
        assert (
            not (car_root / "contextspace" / "active_context.md").exists()
            or (car_root / "contextspace" / "active_context.md").read_text(
                encoding="utf-8"
            )
            == ""
        )

    def test_archive_snapshot_uses_contextspace_source(self, tmp_path: Path) -> None:
        base_repo, worktree_repo = _setup_worktree(tmp_path)

        result = archive_worktree_snapshot(
            base_repo_root=base_repo,
            base_repo_id="base",
            worktree_repo_root=worktree_repo,
            worktree_repo_id="worktree",
            branch="feature/archive-viewer",
            worktree_of="base",
        )

        assert (result.snapshot_path / "contextspace" / "active_context.md").exists()
        assert not (result.snapshot_path / "workspace").exists()


class TestContextspaceSourceHelper:
    def test_prefers_contextspace(self, tmp_path: Path) -> None:
        from codex_autorunner.core.archive import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"
        (car_root / "contextspace").mkdir(parents=True)
        (car_root / "workspace").mkdir(parents=True)

        result = _contextspace_source(car_root)
        assert result.name == "contextspace"

    def test_falls_back_to_workspace(self, tmp_path: Path) -> None:
        from codex_autorunner.core.archive import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"
        (car_root / "workspace").mkdir(parents=True)

        result = _contextspace_source(car_root)
        assert result.name == "workspace"

    def test_returns_contextspace_when_neither_exists(self, tmp_path: Path) -> None:
        from codex_autorunner.core.archive import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"

        result = _contextspace_source(car_root)
        assert result.name == "contextspace"

    def test_symlink_contextspace_preferred_over_workspace_dir(
        self, tmp_path: Path
    ) -> None:
        from codex_autorunner.core.archive import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"
        car_root.mkdir(parents=True)
        link_target = tmp_path / "real_contextspace"
        link_target.mkdir()
        (car_root / "contextspace").symlink_to(link_target)
        (car_root / "workspace").mkdir()

        result = _contextspace_source(car_root)
        assert result.name == "contextspace"
        assert result.is_symlink()


class TestFlowsArchiveHelpersContextspaceSource:
    def test_uses_canonical_owner(self) -> None:
        from codex_autorunner.core.archive import _contextspace_source as canonical
        from codex_autorunner.core.flows.archive_helpers import (
            _contextspace_source as imported,
        )

        assert imported is canonical

    def test_prefers_contextspace(self, tmp_path: Path) -> None:
        from codex_autorunner.core.flows.archive_helpers import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"
        (car_root / "contextspace").mkdir(parents=True)
        (car_root / "workspace").mkdir(parents=True)

        result = _contextspace_source(car_root)
        assert result.name == "contextspace"

    def test_falls_back_to_workspace(self, tmp_path: Path) -> None:
        from codex_autorunner.core.flows.archive_helpers import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"
        (car_root / "workspace").mkdir(parents=True)

        result = _contextspace_source(car_root)
        assert result.name == "workspace"

    def test_returns_contextspace_when_neither_exists(self, tmp_path: Path) -> None:
        from codex_autorunner.core.flows.archive_helpers import _contextspace_source

        car_root = tmp_path / ".codex-autorunner"

        result = _contextspace_source(car_root)
        assert result.name == "contextspace"
