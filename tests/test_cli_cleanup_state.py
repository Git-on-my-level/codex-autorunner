from __future__ import annotations

import types
from pathlib import Path

import typer
from typer.testing import CliRunner

from codex_autorunner.core.archive_retention import ArchivePruneSummary
from codex_autorunner.core.filebox_retention import FileBoxPruneSummary
from codex_autorunner.core.report_retention import PruneSummary
from codex_autorunner.integrations.app_server.retention import WorkspacePruneSummary
from codex_autorunner.surfaces.cli.commands import cleanup as cleanup_cmd

runner = CliRunner()


class TestCleanupStateCLI:
    def test_cleanup_state_rejects_invalid_scope(self, tmp_path: Path) -> None:
        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "invalid"],
        )

        assert result.exit_code != 0
        assert "scope must be one of" in result.output

    def test_cleanup_state_dry_run_shows_prefix_in_report(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        def _fake_prune(*args, **kwargs):
            return ArchivePruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0, pruned_paths=()
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
            _fake_prune,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
            _fake_prune,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
            lambda *a, **kw: FileBoxPruneSummary(
                inbox_kept=1,
                inbox_pruned=0,
                outbox_kept=1,
                outbox_pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
            lambda *a, **kw: PruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            lambda *a, **kw: WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            ),
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "repo", "--dry-run"],
        )

        assert result.exit_code == 0
        assert "DRY RUN:" in result.output


class TestCleanupStateRepoCleanup:
    def test_repo_cleanup_prunes_all_repo_families(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        captured_summaries = {}

        def _fake_prune_worktree_archive_root(path, *, policy, dry_run=False):
            captured_summaries["worktree"] = True
            return ArchivePruneSummary(
                kept=2,
                pruned=1,
                bytes_before=100,
                bytes_after=50,
                pruned_paths=(),
            )

        def _fake_prune_run_archive_root(path, *, policy, dry_run=False):
            captured_summaries["run"] = True
            return ArchivePruneSummary(
                kept=3,
                pruned=2,
                bytes_before=200,
                bytes_after=80,
                pruned_paths=(),
            )

        def _fake_prune_filebox_root(repo_root, *, policy, scope, dry_run=False):
            captured_summaries["filebox"] = True
            return FileBoxPruneSummary(
                inbox_kept=1,
                inbox_pruned=1,
                outbox_kept=1,
                outbox_pruned=1,
                bytes_before=300,
                bytes_after=100,
                pruned_paths=(),
            )

        def _fake_prune_report_directory(path, **kwargs):
            captured_summaries["reports"] = True
            return PruneSummary(kept=5, pruned=2, bytes_before=400, bytes_after=200)

        def _fake_prune_workspace_root(
            workspace_root, *, policy, dry_run=False, **kwargs
        ):
            captured_summaries["workspace"] = True
            return WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=50,
                bytes_after=50,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
            _fake_prune_worktree_archive_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
            _fake_prune_run_archive_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
            _fake_prune_filebox_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
            _fake_prune_report_directory,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            _fake_prune_workspace_root,
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "repo"],
        )

        assert result.exit_code == 0
        assert captured_summaries.get("worktree") is True
        assert captured_summaries.get("run") is True
        assert captured_summaries.get("filebox") is True
        assert captured_summaries.get("reports") is True
        assert captured_summaries.get("workspace") is True

    def test_repo_cleanup_respects_dry_run_flag(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        captured = {"dry_run_values": []}

        def _capture_prune(*args, dry_run=False, **kwargs):
            captured["dry_run_values"].append(dry_run)
            return ArchivePruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0, pruned_paths=()
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
            _capture_prune,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
            _capture_prune,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
            lambda *args, dry_run=False, **kwargs: FileBoxPruneSummary(
                inbox_kept=1,
                inbox_pruned=0,
                outbox_kept=1,
                outbox_pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
            lambda *args, **kwargs: PruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            lambda *args, dry_run=False, **kwargs: WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            ),
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "repo", "--dry-run"],
        )

        assert result.exit_code == 0
        assert all(captured["dry_run_values"])


class TestCleanupStateGlobalCleanup:
    def test_global_cleanup_prunes_global_workspaces(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        captured = {"called": False, "path": None}

        def _fake_prune_workspace_root(
            workspace_root, *, policy, dry_run=False, **kwargs
        ):
            captured["called"] = True
            captured["path"] = workspace_root
            return WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=100,
                bytes_after=100,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            _fake_prune_workspace_root,
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "global"],
        )

        assert result.exit_code == 0
        assert captured["called"] is True

    def test_global_cleanup_respects_dry_run(self, monkeypatch, tmp_path: Path) -> None:
        captured: dict[str, bool | None] = {"dry_run": None}

        def _fake_prune_workspace_root(
            workspace_root, *, policy, dry_run=False, **kwargs
        ):
            captured["dry_run"] = dry_run
            return WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            _fake_prune_workspace_root,
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "global", "--dry-run"],
        )

        assert result.exit_code == 0
        assert captured["dry_run"] is True


class TestCleanupStateByteAccounting:
    def test_dry_run_shows_zero_deleted_bytes_for_families_with_dry_run_support(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        def _fake_prune_worktree_archive_root(path, *, policy, dry_run=False):
            return ArchivePruneSummary(
                kept=1,
                pruned=2,
                bytes_before=1000,
                bytes_after=400,
                pruned_paths=("/tmp/a", "/tmp/b"),
            )

        def _fake_prune_run_archive_root(path, *, policy, dry_run=False):
            return ArchivePruneSummary(
                kept=1,
                pruned=1,
                bytes_before=500,
                bytes_after=200,
                pruned_paths=("/tmp/c",),
            )

        def _fake_prune_filebox_root(repo_root, *, policy, scope, dry_run=False):
            return FileBoxPruneSummary(
                inbox_kept=1,
                inbox_pruned=1,
                outbox_kept=1,
                outbox_pruned=1,
                bytes_before=300,
                bytes_after=100,
                pruned_paths=(),
            )

        def _fake_prune_report_directory(path, **kwargs):
            return PruneSummary(kept=1, pruned=0, bytes_before=200, bytes_after=200)

        def _fake_prune_workspace_root(
            workspace_root, *, policy, dry_run=False, **kwargs
        ):
            return WorkspacePruneSummary(
                kept=1,
                pruned=1,
                bytes_before=100,
                bytes_after=50,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
            _fake_prune_worktree_archive_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
            _fake_prune_run_archive_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
            _fake_prune_filebox_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
            _fake_prune_report_directory,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            _fake_prune_workspace_root,
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "repo", "--dry-run"],
        )

        assert result.exit_code == 0
        assert "DRY RUN:" in result.output
        assert "worktree_archives:" not in result.output


class TestCleanupStateScope:
    def test_scope_repo_only_runs_repo_cleanup(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        captured = {"global_called": False}

        def _fake_prune_workspace_root(
            workspace_root, *, policy, dry_run=False, **kwargs
        ):
            from codex_autorunner.integrations.app_server.retention import (
                resolve_global_workspace_root,
            )

            if workspace_root == resolve_global_workspace_root():
                captured["global_called"] = True
            return WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            _fake_prune_workspace_root,
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
            lambda *a, **kw: ArchivePruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0, pruned_paths=()
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
            lambda *a, **kw: ArchivePruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0, pruned_paths=()
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
            lambda *a, **kw: FileBoxPruneSummary(
                inbox_kept=1,
                inbox_pruned=0,
                outbox_kept=1,
                outbox_pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
            ),
        )
        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
            lambda *a, **kw: PruneSummary(
                kept=1, pruned=0, bytes_before=0, bytes_after=0
            ),
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "repo"],
        )

        assert result.exit_code == 0
        assert not captured["global_called"]

    def test_scope_global_only_runs_global_cleanup(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        captured = {"repo_called": False}

        def _fake_prune_workspace_root(
            workspace_root, *, policy, dry_run=False, **kwargs
        ):
            from codex_autorunner.integrations.app_server.retention import (
                resolve_repo_workspace_root,
            )

            if workspace_root == resolve_repo_workspace_root(tmp_path):
                captured["repo_called"] = True
            return WorkspacePruneSummary(
                kept=1,
                pruned=0,
                bytes_before=0,
                bytes_after=0,
                pruned_paths=(),
                blocked_paths=(),
                blocked_reasons=(),
            )

        monkeypatch.setattr(
            "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
            _fake_prune_workspace_root,
        )

        cleanup_app = typer.Typer()
        cleanup_cmd.register_cleanup_commands(
            cleanup_app,
            require_repo_config=lambda _repo, _hub: types.SimpleNamespace(  # type: ignore[arg-type]
                repo_root=tmp_path,
                config=types.SimpleNamespace(pma=types.SimpleNamespace()),
            ),
        )

        result = runner.invoke(
            cleanup_app,
            ["state", "--repo", str(tmp_path), "--scope", "global"],
        )

        assert result.exit_code == 0
        assert not captured["repo_called"]
