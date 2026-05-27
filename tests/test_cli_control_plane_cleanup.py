from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.manifest import Manifest, ManifestRepo, save_manifest

runner = CliRunner()


def test_cleanup_control_plane_dry_run_json_reports_reclaimable_artifacts(
    hub_root_only: Path,
) -> None:
    repo_root = hub_root_only / "workspace" / "repo"
    state_root = repo_root / ".codex-autorunner"
    state_root.mkdir(parents=True)
    save_manifest(
        hub_root_only / ".codex-autorunner" / "manifest.yml",
        Manifest(
            version=3,
            repos=[ManifestRepo(id="repo", path=Path("workspace/repo"), kind="base")],
        ),
        hub_root_only,
    )
    (state_root / "manifest.yml").write_text("version: 3\nrepos: []\n")
    (state_root / "orchestration.sqlite3").write_bytes(b"stale")

    result = runner.invoke(
        app,
        [
            "cleanup",
            "control-plane",
            "--hub",
            str(hub_root_only),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["candidate_count"] == 2
    assert payload["total_reclaimable_bytes"] > 0
    assert {candidate["path"] for candidate in payload["candidates"]} == {
        "workspace/repo/.codex-autorunner/manifest.yml",
        "workspace/repo/.codex-autorunner/orchestration.sqlite3",
    }
    assert (state_root / "orchestration.sqlite3").exists()


def test_cleanup_control_plane_apply_archives_artifacts(
    hub_root_only: Path,
) -> None:
    repo_root = hub_root_only / "workspace" / "repo"
    state_root = repo_root / ".codex-autorunner"
    state_root.mkdir(parents=True)
    save_manifest(
        hub_root_only / ".codex-autorunner" / "manifest.yml",
        Manifest(
            version=3,
            repos=[ManifestRepo(id="repo", path=Path("workspace/repo"), kind="base")],
        ),
        hub_root_only,
    )
    stale_db = state_root / "orchestration.sqlite3"
    stale_db.write_bytes(b"stale")

    result = runner.invoke(
        app,
        [
            "cleanup",
            "control-plane",
            "--hub",
            str(hub_root_only),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["candidate_count"] == 1
    assert stale_db.exists() is False
    archived_path = hub_root_only / payload["candidates"][0]["archive_path"]
    assert archived_path.read_bytes() == b"stale"
