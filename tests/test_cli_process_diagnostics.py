from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.diagnostics.process_snapshot import (
    ProcessCategory,
    ProcessInfo,
    ProcessSnapshot,
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
    captured = {"force": None, "dry_run": None}

    def _fake_reap(
        _repo_root: Path,
        *,
        dry_run: bool = False,
        max_record_age_seconds: int = 6 * 60 * 60,
        force: bool = False,
    ) -> ReapSummary:
        captured["force"] = force
        captured["dry_run"] = dry_run
        captured["max_record_age_seconds"] = max_record_age_seconds
        return ReapSummary(killed=2, signaled=0, removed=2, skipped=1)

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.cleanup.reap_managed_processes",
        _fake_reap,
    )
    result = runner.invoke(
        app,
        ["cleanup", "processes", "--repo", str(repo), "--force"],
    )

    assert result.exit_code == 0, result.output
    assert captured["force"] is True
    assert captured["max_record_age_seconds"] == 6 * 60 * 60
    assert "killed 2" in result.stdout
    assert "removed 2" in result.stdout
