from __future__ import annotations

import types
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.archive_retention import (
    ArchivePruneSummary,
    RunArchiveRetentionPolicy,
    WorktreeArchiveRetentionPolicy,
)
from codex_autorunner.core.config import ConfigError
from codex_autorunner.core.diagnostics.process_snapshot import (
    ProcessCategory,
    ProcessInfo,
    ProcessSnapshot,
)
from codex_autorunner.core.filebox_retention import (
    FileBoxPruneSummary,
    FileBoxRetentionPolicy,
)
from codex_autorunner.core.force_attestation import (
    FORCE_ATTESTATION_REQUIRED_ERROR,
    FORCE_ATTESTATION_REQUIRED_PHRASE,
)
from codex_autorunner.core.managed_processes import ReapSummary
from codex_autorunner.surfaces.cli.commands import cleanup as cleanup_cmd

runner = CliRunner()


def test_doctor_processes_json_includes_snapshot_and_registry(
    monkeypatch, repo: Path
) -> None:
    from codex_autorunner.core.managed_processes import ProcessRecord
    from codex_autorunner.surfaces.cli.commands import doctor as doctor_cmd

    snapshot = ProcessSnapshot(
        opencode_processes=[
            ProcessInfo(
                pid=1234,
                ppid=1000,
                pgid=1000,
                command="opencode serve",
                category=ProcessCategory.OPENCODE,
            )
        ],
        app_server_processes=[],
        other_processes=[],
    )
    records = [
        ProcessRecord(
            kind="opencode",
            workspace_id="ws",
            pid=1234,
            pgid=1000,
            base_url="http://127.0.0.1:8000",
            command=["opencode", "serve"],
            owner_pid=999,
            started_at="2025-01-01T00:00:00Z",
            metadata={"workspace_root": str(repo / "repo")},
        )
    ]
    monkeypatch.setattr(doctor_cmd, "collect_processes", lambda: snapshot)
    monkeypatch.setattr(doctor_cmd, "list_process_records", lambda _repo: records)

    result = runner.invoke(
        app,
        [
            "doctor",
            "processes",
            "--repo",
            str(repo),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert "snapshot" in payload
    assert payload["snapshot"]["opencode"][0]["pid"] == 1234
    assert "registry" in payload
    assert payload["registry"]["counts_by_kind"]["opencode"] == 1
    assert payload["registry"]["records"][0]["record_key"] == "ws"
    assert (
        str(repo / ".codex-autorunner" / "processes" / "opencode" / "ws.json")
        in payload["registry"]["records"][0]["path"]
    )


def test_cleanup_processes_passes_force_flag(monkeypatch, repo: Path) -> None:
    captured = {"force": None, "dry_run": None, "force_attestation": None}

    def _fake_reap(
        _repo_root: Path,
        *,
        dry_run: bool = False,
        max_record_age_seconds: int = 6 * 60 * 60,
        force: bool = False,
        force_attestation=None,
    ) -> ReapSummary:
        captured["force"] = force
        captured["dry_run"] = dry_run
        captured["max_record_age_seconds"] = max_record_age_seconds
        captured["force_attestation"] = force_attestation
        return ReapSummary(killed=2, signaled=0, removed=2, skipped=1)

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.reap_managed_processes",
        _fake_reap,
    )
    result = runner.invoke(
        app,
        [
            "cleanup",
            "processes",
            "--repo",
            str(repo),
            "--force",
            "--force-attestation",
            "cleanup managed processes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["force"] is True
    assert captured["max_record_age_seconds"] == 6 * 60 * 60
    assert captured["force_attestation"] == {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": "cleanup managed processes",
        "target_scope": f"cleanup.processes:{repo}",
    }
    assert "killed 2" in result.stdout
    assert "removed 2" in result.stdout


def test_cleanup_processes_force_requires_attestation(repo: Path) -> None:
    result = runner.invoke(
        app,
        ["cleanup", "processes", "--repo", str(repo), "--force"],
    )

    assert result.exit_code == 1, result.output
    error_text = result.output or str(result.exception)
    assert FORCE_ATTESTATION_REQUIRED_ERROR in error_text


def test_cleanup_archives_uses_repo_retention_policy(monkeypatch, repo: Path) -> None:
    captured: dict[str, object] = {}
    cleanup_app = typer.Typer()
    cleanup_cmd.register_cleanup_commands(
        cleanup_app,
        require_repo_config=lambda _repo, _hub: types.SimpleNamespace(
            repo_root=repo,
            config=types.SimpleNamespace(
                pma=types.SimpleNamespace(
                    worktree_archive_max_snapshots_per_repo=7,
                    worktree_archive_max_age_days=21,
                    worktree_archive_max_total_bytes=123456,
                    run_archive_max_entries=9,
                    run_archive_max_age_days=11,
                    run_archive_max_total_bytes=654321,
                )
            ),
        ),
    )

    def _fake_prune_worktrees(path: Path, *, policy, preserve_paths=(), dry_run=False):
        captured["worktrees_path"] = path
        captured["worktrees_policy"] = policy
        captured["worktrees_dry_run"] = dry_run
        return ArchivePruneSummary(
            kept=4, pruned=1, bytes_before=100, bytes_after=40, pruned_paths=()
        )

    def _fake_prune_runs(path: Path, *, policy, preserve_paths=(), dry_run=False):
        captured["runs_path"] = path
        captured["runs_policy"] = policy
        captured["runs_dry_run"] = dry_run
        return ArchivePruneSummary(
            kept=9, pruned=2, bytes_before=200, bytes_after=60, pruned_paths=()
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
        _fake_prune_worktrees,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
        _fake_prune_runs,
    )

    result = runner.invoke(
        cleanup_app,
        [
            "archives",
            "--repo",
            str(repo),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        captured["worktrees_path"]
        == repo / ".codex-autorunner" / "archive" / "worktrees"
    )
    assert captured["runs_path"] == repo / ".codex-autorunner" / "archive" / "runs"
    assert captured["worktrees_policy"] == WorktreeArchiveRetentionPolicy(
        max_snapshots_per_repo=7,
        max_age_days=21,
        max_total_bytes=123456,
    )
    assert captured["runs_policy"] == RunArchiveRetentionPolicy(
        max_entries=9,
        max_age_days=11,
        max_total_bytes=654321,
    )
    assert captured["worktrees_dry_run"] is True
    assert captured["runs_dry_run"] is True
    assert "Dry run: worktrees:" in result.stdout
    assert "runs:" in result.stdout


def test_cleanup_filebox_uses_repo_retention_policy(monkeypatch, repo: Path) -> None:
    captured: dict[str, object] = {}
    cleanup_app = typer.Typer()
    cleanup_cmd.register_cleanup_commands(
        cleanup_app,
        require_repo_config=lambda _repo, _hub: types.SimpleNamespace(
            repo_root=repo,
            config=types.SimpleNamespace(
                pma=types.SimpleNamespace(
                    filebox_inbox_max_age_days=7,
                    filebox_outbox_max_age_days=7,
                )
            ),
        ),
    )

    def _fake_prune_filebox(
        path: Path, *, policy, scope="both", dry_run=False, now=None
    ):
        captured["path"] = path
        captured["policy"] = policy
        captured["scope"] = scope
        captured["dry_run"] = dry_run
        captured["now"] = now
        return FileBoxPruneSummary(
            inbox_kept=4,
            inbox_pruned=1,
            outbox_kept=8,
            outbox_pruned=2,
            bytes_before=300,
            bytes_after=90,
            pruned_paths=(),
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
        _fake_prune_filebox,
    )

    result = runner.invoke(
        cleanup_app,
        [
            "filebox",
            "--repo",
            str(repo),
            "--scope",
            "outbox",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["path"] == repo
    assert captured["policy"] == FileBoxRetentionPolicy(
        inbox_max_age_days=7,
        outbox_max_age_days=7,
    )
    assert captured["scope"] == "outbox"
    assert captured["dry_run"] is True
    assert "Dry run: inbox: kept=4 pruned=1" in result.stdout
    assert "outbox: kept=8 pruned=2" in result.stdout


@pytest.mark.parametrize(
    "failure",
    [
        ConfigError("missing config"),
        ValueError("broken config"),
    ],
)
def test_doctor_processes_skips_opencode_lifecycle_when_repo_config_missing(
    monkeypatch, repo: Path, failure: Exception
) -> None:
    from codex_autorunner.surfaces.cli.commands import doctor as doctor_cmd

    snapshot = ProcessSnapshot(
        opencode_processes=[], app_server_processes=[], other_processes=[]
    )

    monkeypatch.setattr(doctor_cmd, "collect_processes", lambda: snapshot)
    monkeypatch.setattr(doctor_cmd, "find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        doctor_cmd,
        "summarize_opencode_lifecycle",
        lambda _repo: (_ for _ in ()).throw(failure),
    )

    result = runner.invoke(
        app,
        [
            "doctor",
            "processes",
            "--repo",
            str(repo),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["opencode_lifecycle"] == {}


def test_cleanup_state_dry_run_reports_all_buckets(monkeypatch, repo: Path) -> None:
    captured = {"calls": []}
    cleanup_app = typer.Typer()
    cleanup_cmd.register_cleanup_commands(
        cleanup_app,
        require_repo_config=lambda _repo, _hub: types.SimpleNamespace(
            repo_root=repo,
            config=types.SimpleNamespace(
                pma=types.SimpleNamespace(
                    worktree_archive_max_snapshots_per_repo=3,
                    worktree_archive_max_age_days=14,
                    worktree_archive_max_total_bytes=100000,
                    run_archive_max_entries=5,
                    run_archive_max_age_days=7,
                    run_archive_max_total_bytes=50000,
                    filebox_inbox_max_age_days=3,
                    filebox_outbox_max_age_days=3,
                    app_server_workspace_max_age_days=7,
                )
            ),
        ),
    )

    def _fake_prune_worktrees(path: Path, *, policy, preserve_paths=(), dry_run=False):
        captured["calls"].append(("worktrees", dry_run))
        return ArchivePruneSummary(
            kept=2,
            pruned=3,
            bytes_before=500,
            bytes_after=200,
            pruned_paths=("a", "b", "c"),
        )

    def _fake_prune_runs(path: Path, *, policy, preserve_paths=(), dry_run=False):
        captured["calls"].append(("runs", dry_run))
        return ArchivePruneSummary(
            kept=1, pruned=2, bytes_before=300, bytes_after=100, pruned_paths=("d", "e")
        )

    def _fake_prune_filebox(
        path: Path, *, policy, scope="both", dry_run=False, now=None
    ):
        captured["calls"].append(("filebox", scope, dry_run))
        return FileBoxPruneSummary(
            inbox_kept=2,
            inbox_pruned=1,
            outbox_kept=3,
            outbox_pruned=2,
            bytes_before=200,
            bytes_after=80,
            pruned_paths=("f", "g", "h"),
        )

    def _fake_prune_reports(
        path: Path, *, max_history_files=10, max_total_bytes=1000000, dry_run=False
    ):
        captured["calls"].append(("reports",))
        from codex_autorunner.core.report_retention import PruneSummary

        return PruneSummary(kept=4, pruned=1, bytes_before=150, bytes_after=100)

    def _fake_prune_workspace(
        workspace_root,
        *,
        policy,
        active_workspace_ids,
        locked_workspace_ids,
        current_workspace_ids,
        dry_run=False,
        now=None,
        scope=None,
    ):
        captured["calls"].append(("workspace", scope, dry_run))
        from codex_autorunner.integrations.app_server.retention import (
            WorkspacePruneSummary,
        )

        return WorkspacePruneSummary(
            kept=1,
            pruned=2,
            bytes_before=400,
            bytes_after=100,
            pruned_paths=("i", "j"),
            blocked_paths=(),
            blocked_reasons=(),
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
        _fake_prune_worktrees,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
        _fake_prune_runs,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
        _fake_prune_filebox,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
        _fake_prune_reports,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
        _fake_prune_workspace,
    )

    result = runner.invoke(
        cleanup_app,
        ["state", "--repo", str(repo), "--dry-run", "--scope", "repo"],
    )

    assert result.exit_code == 0, result.output
    assert ("worktrees", True) in captured["calls"]
    assert ("runs", True) in captured["calls"]
    assert ("filebox", "both", True) in captured["calls"]
    assert ("reports",) in captured["calls"]
    workspace_calls = [c for c in captured["calls"] if c[0] == "workspace"]
    assert len(workspace_calls) >= 1
    assert workspace_calls[0][2] is True
    assert "DRY RUN:" in result.stdout
    assert "CAR State Cleanup Report" in result.stdout


def test_cleanup_state_scope_global_includes_global_workspaces(
    monkeypatch, repo: Path
) -> None:
    captured = {"calls": []}
    cleanup_app = typer.Typer()
    cleanup_cmd.register_cleanup_commands(
        cleanup_app,
        require_repo_config=lambda _repo, _hub: types.SimpleNamespace(
            repo_root=repo,
            config=types.SimpleNamespace(
                pma=types.SimpleNamespace(
                    app_server_workspace_max_age_days=7,
                )
            ),
        ),
    )

    def _fake_prune_workspace(
        workspace_root,
        *,
        policy,
        active_workspace_ids,
        locked_workspace_ids,
        current_workspace_ids,
        dry_run=False,
        now=None,
        scope=None,
    ):
        captured["calls"].append(("workspace", scope, dry_run))
        from codex_autorunner.integrations.app_server.retention import (
            WorkspacePruneSummary,
        )

        return WorkspacePruneSummary(
            kept=0,
            pruned=0,
            bytes_before=0,
            bytes_after=0,
            pruned_paths=(),
            blocked_paths=(),
            blocked_reasons=(),
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
        _fake_prune_workspace,
    )

    result = runner.invoke(
        cleanup_app,
        ["state", "--repo", str(repo), "--scope", "global"],
    )

    assert result.exit_code == 0, result.output
    global_calls = [
        c for c in captured["calls"] if c[1] is not None and c[1].value == "global"
    ]
    assert len(global_calls) >= 1


def test_cleanup_state_scope_all_includes_both_repo_and_global(
    monkeypatch, repo: Path
) -> None:
    captured = {"scopes": []}
    cleanup_app = typer.Typer()
    cleanup_cmd.register_cleanup_commands(
        cleanup_app,
        require_repo_config=lambda _repo, _hub: types.SimpleNamespace(
            repo_root=repo,
            config=types.SimpleNamespace(
                pma=types.SimpleNamespace(
                    worktree_archive_max_snapshots_per_repo=3,
                    worktree_archive_max_age_days=14,
                    worktree_archive_max_total_bytes=100000,
                    run_archive_max_entries=5,
                    run_archive_max_age_days=7,
                    run_archive_max_total_bytes=50000,
                    filebox_inbox_max_age_days=3,
                    filebox_outbox_max_age_days=3,
                    app_server_workspace_max_age_days=7,
                )
            ),
        ),
    )

    def _fake_prune_worktrees(path: Path, *, policy, preserve_paths=(), dry_run=False):
        return ArchivePruneSummary(
            kept=0, pruned=0, bytes_before=0, bytes_after=0, pruned_paths=()
        )

    def _fake_prune_runs(path: Path, *, policy, preserve_paths=(), dry_run=False):
        return ArchivePruneSummary(
            kept=0, pruned=0, bytes_before=0, bytes_after=0, pruned_paths=()
        )

    def _fake_prune_filebox(
        path: Path, *, policy, scope="both", dry_run=False, now=None
    ):
        return FileBoxPruneSummary(
            inbox_kept=0,
            inbox_pruned=0,
            outbox_kept=0,
            outbox_pruned=0,
            bytes_before=0,
            bytes_after=0,
            pruned_paths=(),
        )

    def _fake_prune_reports(
        path: Path, *, max_history_files=10, max_total_bytes=1000000, dry_run=False
    ):
        from codex_autorunner.core.report_retention import PruneSummary

        return PruneSummary(kept=0, pruned=0, bytes_before=0, bytes_after=0)

    def _fake_prune_workspace(
        workspace_root,
        *,
        policy,
        active_workspace_ids,
        locked_workspace_ids,
        current_workspace_ids,
        dry_run=False,
        now=None,
        scope=None,
    ):
        if scope is not None:
            captured["scopes"].append(
                scope.value if hasattr(scope, "value") else str(scope)
            )
        from codex_autorunner.integrations.app_server.retention import (
            WorkspacePruneSummary,
        )

        return WorkspacePruneSummary(
            kept=0,
            pruned=0,
            bytes_before=0,
            bytes_after=0,
            pruned_paths=(),
            blocked_paths=(),
            blocked_reasons=(),
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_worktree_archive_root",
        _fake_prune_worktrees,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_run_archive_root",
        _fake_prune_runs,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_filebox_root",
        _fake_prune_filebox,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_report_directory",
        _fake_prune_reports,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.prune_workspace_root",
        _fake_prune_workspace,
    )

    result = runner.invoke(
        cleanup_app,
        ["state", "--repo", str(repo), "--scope", "all"],
    )

    assert result.exit_code == 0, result.output
    assert "repo" in captured["scopes"]
    assert "global" in captured["scopes"]
