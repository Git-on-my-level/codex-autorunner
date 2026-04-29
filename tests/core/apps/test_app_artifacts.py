from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.apps import (
    AppArtifactCandidate,
    AppArtifactError,
    collect_before_chat_wrapup_artifacts,
    compute_bundle_sha,
    get_installed_app,
    register_app_artifact_candidates,
    resolve_app_runtime_artifact_path,
)
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.archive_helpers import archive_flow_run_artifacts
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.state_roots import resolve_repo_state_root


def _write_installed_wrapup_app(
    repo_root: Path,
    *,
    app_id: str = "local.wrapup",
    hook_artifacts: tuple[str, ...] = ("artifacts/summary.md",),
) -> Path:
    state_root = resolve_repo_state_root(repo_root)
    app_root = state_root / "apps" / app_id
    bundle_root = app_root / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "schema_version: 1",
        f"id: {app_id}",
        "name: Wrapup App",
        "version: 1.0.0",
        "hooks:",
        "  before_chat_wrapup:",
        "    - artifacts:",
    ]
    for artifact in hook_artifacts:
        manifest_lines.append(f'        - "{artifact}"')
    manifest_text = "\n".join(manifest_lines) + "\n"
    manifest_path = bundle_root / "car-app.yaml"
    manifest_path.write_text(manifest_text, encoding="utf-8")

    bundle_sha = compute_bundle_sha(bundle_root)
    lock_payload = {
        "id": app_id,
        "version": "1.0.0",
        "source_repo_id": "local",
        "source_url": "https://example.invalid/apps.git",
        "source_path": f"apps/{app_id}",
        "source_ref": "main",
        "commit_sha": "deadbeef",
        "manifest_sha": "manifest-sha",
        "bundle_sha": bundle_sha,
        "trusted": True,
        "installed_at": "2026-04-28T00:00:00Z",
    }
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "state").mkdir(exist_ok=True)
    (app_root / "artifacts").mkdir(exist_ok=True)
    (app_root / "logs").mkdir(exist_ok=True)
    (app_root / "app.lock.json").write_text(
        json.dumps(lock_payload, indent=2),
        encoding="utf-8",
    )
    return app_root


def test_resolve_app_runtime_artifact_path_rejects_bundle_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_installed_wrapup_app(repo_root, hook_artifacts=("bundle/car-app.yaml",))
    installed = get_installed_app(repo_root, "local.wrapup")
    assert installed is not None

    with pytest.raises(AppArtifactError):
        resolve_app_runtime_artifact_path(
            installed,
            "bundle/car-app.yaml",
            allowed_dir_names=("artifacts", "state", "logs"),
        )


def test_register_app_artifact_candidates_persists_generic_metadata(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifact_path = repo_root / "artifact.md"
    artifact_path.write_text("# summary\n", encoding="utf-8")
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run("run-123", "ticket_flow", input_data={}, state={})

    created = register_app_artifact_candidates(
        repo_root,
        "run-123",
        [
            AppArtifactCandidate(
                app_id="local.wrapup",
                app_version="1.0.0",
                tool_id="render-card",
                hook_point="after_flow_terminal",
                kind="markdown",
                label="Summary",
                relative_path="artifacts/summary.md",
                absolute_path=artifact_path.resolve(),
            )
        ],
    )

    assert len(created) == 1
    assert created[0].metadata["app_id"] == "local.wrapup"
    assert created[0].metadata["app_version"] == "1.0.0"
    assert created[0].metadata["tool_id"] == "render-card"
    assert created[0].metadata["hook_point"] == "after_flow_terminal"
    assert created[0].metadata["label"] == "Summary"


def test_collect_before_chat_wrapup_artifacts_respects_allowed_roots_and_size(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    app_root = _write_installed_wrapup_app(
        repo_root,
        hook_artifacts=(
            "artifacts/summary.md",
            "state/note.txt",
            "logs/large.bin",
        ),
    )
    (app_root / "artifacts" / "summary.md").write_text("summary", encoding="utf-8")
    (app_root / "state" / "note.txt").write_text("note", encoding="utf-8")
    (app_root / "logs" / "large.bin").write_bytes(b"x" * 128)

    collected = collect_before_chat_wrapup_artifacts(
        repo_root,
        max_file_size_bytes=64,
    )

    assert [item.relative_path for item in collected] == [
        "artifacts/summary.md",
        "state/note.txt",
    ]


def test_archive_flow_run_artifacts_copies_registered_app_artifacts(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)
    app_root = _write_installed_wrapup_app(repo_root)
    artifact_path = app_root / "artifacts" / "summary.md"
    artifact_path.write_text("summary", encoding="utf-8")

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run("run-archive", "ticket_flow", input_data={}, state={})
        store.update_flow_run_status("run-archive", FlowRunStatus.COMPLETED)
        store.create_artifact(
            artifact_id="art-1",
            run_id="run-archive",
            kind="markdown",
            path=str(artifact_path.resolve()),
            metadata={
                "app_id": "local.wrapup",
                "app_version": "1.0.0",
                "tool_id": "render-card",
                "label": "Summary",
                "kind": "markdown",
                "relative_path": "artifacts/summary.md",
            },
        )

    summary = archive_flow_run_artifacts(
        repo_root,
        run_id="run-archive",
        force=False,
        delete_run=False,
    )

    assert summary["archived_app_artifacts"] == 1
    archived_copy = (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / "run-archive"
        / "app_artifacts"
        / "local.wrapup"
        / "summary.md"
    )
    assert archived_copy.exists()
