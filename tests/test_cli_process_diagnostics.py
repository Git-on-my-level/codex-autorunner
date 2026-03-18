from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.archive_retention import ArchivePruneSummary
from codex_autorunner.core.config import ConfigError
from codex_autorunner.core.diagnostics.process_snapshot import (
    ProcessCategory,
    ProcessInfo,
    ProcessSnapshot,
)
from codex_autorunner.core.force_attestation import (
    FORCE_ATTESTATION_REQUIRED_ERROR,
    FORCE_ATTESTATION_REQUIRED_PHRASE,
)
from codex_autorunner.core.managed_processes import ReapSummary

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
        app,
        [
            "cleanup",
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
    assert captured["worktrees_dry_run"] is True
    assert captured["runs_dry_run"] is True
    assert "Dry run: worktrees:" in result.stdout
    assert "runs:" in result.stdout


def test_doctor_processes_skips_opencode_lifecycle_when_repo_config_missing(
    monkeypatch, repo: Path
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
        lambda _repo: (_ for _ in ()).throw(ConfigError("missing config")),
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
