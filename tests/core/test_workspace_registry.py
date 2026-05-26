from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.hub_topology import (
    ControlPlaneRole,
    HubTopologyRepository,
)
from codex_autorunner.manifest import Manifest, ManifestRepo, save_manifest


def _repository(hub_root: Path) -> HubTopologyRepository:
    return HubTopologyRepository(
        hub_root=hub_root,
        manifest_path=hub_root / ".codex-autorunner" / "manifest.yml",
    )


def _save_manifest(hub_root: Path, manifest: Manifest) -> None:
    save_manifest(
        hub_root / ".codex-autorunner" / "manifest.yml",
        manifest,
        hub_root,
    )


def test_workspace_registry_classifies_hub_owned_base_and_worktree(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    worktree_root = hub_root / "worktrees" / "repo--feature"
    repo_root.mkdir(parents=True)
    worktree_root.mkdir(parents=True)
    _save_manifest(
        hub_root,
        Manifest(
            version=3,
            repos=[
                ManifestRepo(
                    id="repo",
                    path=Path("workspace/repo"),
                    kind="base",
                    branch="main",
                ),
                ManifestRepo(
                    id="repo--feature",
                    path=Path("worktrees/repo--feature"),
                    kind="worktree",
                    worktree_of="repo",
                    branch="feature",
                ),
            ],
        ),
    )

    registry = _repository(hub_root).workspace_registry()

    by_id = {entry.repo_id: entry for entry in registry}
    assert by_id["repo"].control_plane_role == ControlPlaneRole.HUB_OWNED
    assert by_id["repo"].resource_kind == "repo"
    assert by_id["repo"].default_branch == "main"
    assert by_id["repo--feature"].control_plane_role == ControlPlaneRole.HUB_OWNED
    assert by_id["repo--feature"].resource_kind == "worktree"
    assert by_id["repo--feature"].worktree_of == "repo"


def test_nested_manifest_and_local_orchestration_db_are_non_authoritative(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    local_state = repo_root / ".codex-autorunner"
    local_state.mkdir(parents=True)
    (local_state / "manifest.yml").write_text(
        "version: 3\nrepos: []\n",
        encoding="utf-8",
    )
    (local_state / "orchestration.sqlite3").write_bytes(b"stale")
    _save_manifest(
        hub_root,
        Manifest(
            version=3,
            repos=[ManifestRepo(id="repo", path=Path("workspace/repo"), kind="base")],
        ),
    )

    entry = _repository(hub_root).classify_workspace_path(repo_root)

    assert entry.control_plane_role == ControlPlaneRole.HUB_OWNED
    assert entry.authoritative_hub_root == hub_root.resolve()
    assert {artifact.kind for artifact in entry.non_authoritative_artifacts} == {
        "manifest",
        "orchestration_db",
    }


def test_explicit_hub_root_classifies_as_standalone_hub(tmp_path: Path) -> None:
    hub_root = tmp_path / "standalone"
    _save_manifest(hub_root, Manifest(version=3, repos=[]))
    (hub_root / ".codex-autorunner" / "orchestration.sqlite3").write_bytes(b"live")

    entry = _repository(hub_root).classify_workspace_path(hub_root)

    assert entry.control_plane_role == ControlPlaneRole.STANDALONE_HUB
    assert entry.authoritative_hub_root == hub_root.resolve()
    assert entry.non_authoritative_artifacts == ()


def test_unlisted_hub_local_path_is_repo_local_data_not_authority(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    observed = hub_root / "workspace" / "scratch"
    (observed / ".codex-autorunner").mkdir(parents=True)
    _save_manifest(hub_root, Manifest(version=3, repos=[]))

    entry = _repository(hub_root).classify_workspace_path(observed)

    assert entry.control_plane_role == ControlPlaneRole.REPO_LOCAL_DATA
    assert entry.authoritative_hub_root is None
    assert entry.has_car_state is True


def test_external_path_and_archived_path_are_diagnostic_only(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    external = tmp_path / "external"
    archived = hub_root / "archives" / "repo"
    external.mkdir()
    archived.mkdir(parents=True)
    _save_manifest(hub_root, Manifest(version=3, repos=[]))
    repository = _repository(hub_root)

    external_entry = repository.classify_workspace_path(external)
    archived_entry = repository.classify_workspace_path(archived)

    assert external_entry.control_plane_role == ControlPlaneRole.FOREIGN
    assert external_entry.authoritative_hub_root is None
    assert archived_entry.control_plane_role == ControlPlaneRole.ARCHIVED
    assert archived_entry.authoritative_hub_root is None


def test_binding_context_uses_registry_without_nested_manifest_authority(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    nested_state = repo_root / ".codex-autorunner"
    nested_state.mkdir(parents=True)
    (nested_state / "manifest.yml").write_text(
        "version: 3\nrepos: []\n",
        encoding="utf-8",
    )
    _save_manifest(
        hub_root,
        Manifest(
            version=3,
            repos=[ManifestRepo(id="repo", path=Path("workspace/repo"), kind="base")],
        ),
    )

    assert _repository(hub_root).binding_context_for_workspace(repo_root) == (
        hub_root.resolve(),
        "repo",
    )
