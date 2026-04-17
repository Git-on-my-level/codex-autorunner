from __future__ import annotations

import types
from pathlib import Path

import typer
from typer.testing import CliRunner

from codex_autorunner.core.pytest_temp_cleanup import TempCleanupSummary
from codex_autorunner.surfaces.cli.commands import cleanup as cleanup_cmd

runner = CliRunner()


def test_cleanup_pytest_tmp_reports_summary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cleanup_cmd,
        "cleanup_repo_pytest_temp_runs",
        lambda repo_root, dry_run=False: TempCleanupSummary(
            scanned=3,
            deleted=2,
            active=1,
            failed=0,
            bytes_before=300,
            bytes_after=100,
            deleted_paths=(),
            active_paths=(tmp_path / "active-run",),
            failed_paths=(),
            active_processes=(),
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

    result = runner.invoke(cleanup_app, ["pytest-tmp", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert "pytest tmp cleanup:" in result.output
    assert "scanned=3" in result.output
    assert "deleted=2" in result.output
    assert "active=1" in result.output
    assert f"ACTIVE {tmp_path / 'active-run'}" in result.output


def test_cleanup_temp_roots_reports_summary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cleanup_cmd,
        "cleanup_repo_managed_temp_paths",
        lambda repo_root, dry_run=False: TempCleanupSummary(
            scanned=4,
            deleted=3,
            active=1,
            failed=0,
            bytes_before=400,
            bytes_after=100,
            deleted_paths=(),
            active_paths=(tmp_path / "car-hub-profile-20260414",),
            failed_paths=(),
            active_processes=(),
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

    result = runner.invoke(cleanup_app, ["temp-roots", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert "temp root cleanup:" in result.output
    assert "scanned=4" in result.output
    assert "deleted=3" in result.output
    assert "active=1" in result.output
    assert f"ACTIVE {tmp_path / 'car-hub-profile-20260414'}" in result.output
