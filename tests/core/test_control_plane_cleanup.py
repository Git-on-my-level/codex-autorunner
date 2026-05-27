from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.control_plane_cleanup import (
    apply_control_plane_cleanup,
    plan_control_plane_cleanup,
)
from codex_autorunner.manifest import Manifest, ManifestRepo, save_manifest


def _save_manifest(hub_root: Path, manifest: Manifest) -> Path:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    save_manifest(manifest_path, manifest, hub_root)
    return manifest_path


def test_control_plane_cleanup_reports_nested_manifest_and_stale_dbs(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    state_root = repo_root / ".codex-autorunner"
    state_root.mkdir(parents=True)
    manifest_path = _save_manifest(
        hub_root,
        Manifest(
            version=3,
            repos=[ManifestRepo(id="repo", path=Path("workspace/repo"), kind="base")],
        ),
    )
    (state_root / "manifest.yml").write_text("version: 3\nrepos: []\n")
    (state_root / "orchestration.sqlite3").write_bytes(b"stale-db")
    (state_root / "orchestration.sqlite3-wal").write_bytes(b"wal")
    (state_root / "hub_projection.sqlite3").write_bytes(b"projection")
    (state_root / "orchestration-compatibility.json").write_text("{}\n")
    (state_root / "orchestration.sqlite3.migrate.lock").write_text("lock\n")

    report = plan_control_plane_cleanup(
        hub_root=hub_root,
        manifest_path=manifest_path,
        archive_stamp="stamp",
    )

    assert report.dry_run is True
    assert report.total_reclaimable_bytes > 0
    assert {candidate.kind for candidate in report.candidates} == {
        "manifest",
        "orchestration_db",
        "orchestration_db_wal",
        "hub_projection_db",
        "orchestration_compatibility_metadata",
        "orchestration_migration_lock",
    }
    assert all(
        candidate.control_plane_role.value == "hub_owned"
        for candidate in report.candidates
    )
    assert all(
        candidate.proposed_action == "archive" for candidate in report.candidates
    )


def test_control_plane_cleanup_apply_archives_only_control_plane_artifacts(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    state_root = repo_root / ".codex-autorunner"
    tickets_dir = state_root / "tickets"
    context_dir = state_root / "contextspace"
    tickets_dir.mkdir(parents=True)
    context_dir.mkdir()
    (tickets_dir / "TICKET-001.md").write_text("# keep\n")
    (context_dir / "active_context.md").write_text("keep\n")
    manifest_path = _save_manifest(
        hub_root,
        Manifest(
            version=3,
            repos=[ManifestRepo(id="repo", path=Path("workspace/repo"), kind="base")],
        ),
    )
    stale_db = state_root / "orchestration.sqlite3"
    stale_db.write_bytes(b"stale-db")

    dry_run = plan_control_plane_cleanup(
        hub_root=hub_root,
        manifest_path=manifest_path,
        archive_stamp="stamp",
    )
    applied = apply_control_plane_cleanup(dry_run)

    assert stale_db.exists() is False
    assert (
        hub_root
        / ".codex-autorunner/archive/control-plane-cleanup/stamp/workspace/repo/.codex-autorunner/orchestration.sqlite3"
    ).read_bytes() == b"stale-db"
    assert (tickets_dir / "TICKET-001.md").read_text() == "# keep\n"
    assert (context_dir / "active_context.md").read_text() == "keep\n"
    assert applied.dry_run is False
    assert applied.errors == ()


def test_control_plane_cleanup_never_touches_standalone_hub(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    manifest_path = _save_manifest(hub_root, Manifest(version=3, repos=[]))
    live_db = hub_root / ".codex-autorunner" / "orchestration.sqlite3"
    live_db.write_bytes(b"live")

    report = plan_control_plane_cleanup(
        hub_root=hub_root,
        manifest_path=manifest_path,
        archive_stamp="stamp",
    )

    assert report.candidates == ()
    assert live_db.read_bytes() == b"live"
    assert {(item["path"], item["reason"]) for item in report.skipped} == {
        (
            ".codex-autorunner/manifest.yml",
            "explicit standalone hub control plane",
        ),
        (
            ".codex-autorunner/orchestration.sqlite3",
            "explicit standalone hub control plane",
        ),
    }
