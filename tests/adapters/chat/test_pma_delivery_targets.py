from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.adapters.chat.pma_delivery_targets import (
    PmaChatSurfaceBinding,
    select_bound_pma_targets,
    select_explicit_pma_target,
    select_primary_pma_target,
)


@pytest.mark.parametrize("surface_key", ["discord-channel", "100:root:repo"])
def test_select_explicit_pma_target_requires_existing_surface_key(
    surface_key: str,
) -> None:
    bindings = (
        PmaChatSurfaceBinding(surface_key=surface_key, is_primary_pma=True),
        PmaChatSurfaceBinding(surface_key="other"),
    )

    target = select_explicit_pma_target(
        surface_key=f" {surface_key} ",
        bindings=bindings,
    )

    assert target is not None
    assert target.surface_key == surface_key
    assert target.workspace_root is None
    assert select_explicit_pma_target(surface_key="missing", bindings=bindings) is None


@pytest.mark.parametrize(
    "surface_keys", [("discord-a", "discord-b"), ("10:root", "20:5")]
)
def test_select_bound_pma_targets_filters_repo_workspace_and_deduplicates(
    tmp_path: Path,
    surface_keys: tuple[str, str],
) -> None:
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    bindings = (
        PmaChatSurfaceBinding(
            surface_key=surface_keys[1],
            workspace_path=str(workspace),
            repo_id=None,
        ),
        PmaChatSurfaceBinding(
            surface_key=surface_keys[0],
            workspace_path=str(workspace),
            repo_id="repo-1",
        ),
        PmaChatSurfaceBinding(
            surface_key=surface_keys[0],
            workspace_path=str(workspace),
            repo_id="repo-1",
        ),
        PmaChatSurfaceBinding(
            surface_key="primary",
            workspace_path=str(workspace),
            repo_id="repo-1",
            is_primary_pma=True,
        ),
        PmaChatSurfaceBinding(
            surface_key="wrong-repo",
            workspace_path=str(workspace),
            repo_id="repo-2",
        ),
        PmaChatSurfaceBinding(
            surface_key="wrong-workspace",
            workspace_path=str(other_workspace),
            repo_id="repo-1",
        ),
    )

    targets = select_bound_pma_targets(
        workspace_root=str(workspace),
        repo_id="repo-1",
        bindings=bindings,
        repo_id_by_workspace={str(workspace.resolve()): "repo-1"},
    )

    assert [target.surface_key for target in targets] == sorted(surface_keys)
    assert {target.workspace_root for target in targets} == {str(workspace)}


@pytest.mark.parametrize(
    "surface_keys", [("discord-old", "discord-new"), ("10:root", "20:5")]
)
def test_select_primary_pma_target_uses_current_and_previous_repo_fields(
    tmp_path: Path,
    surface_keys: tuple[str, str],
) -> None:
    current_workspace = tmp_path / "current"
    previous_workspace = tmp_path / "previous"
    current_workspace.mkdir()
    previous_workspace.mkdir()
    bindings = (
        PmaChatSurfaceBinding(
            surface_key=surface_keys[0],
            workspace_path=str(current_workspace),
            repo_id="repo-2",
            is_primary_pma=True,
            previous_workspace_path=str(previous_workspace),
            previous_repo_id=None,
            updated_at="2026-05-17T10:00:00Z",
        ),
        PmaChatSurfaceBinding(
            surface_key=surface_keys[1],
            workspace_path=str(current_workspace),
            repo_id="repo-2",
            is_primary_pma=True,
            previous_workspace_path=str(previous_workspace),
            previous_repo_id="repo-1",
            updated_at="2026-05-17T11:00:00Z",
        ),
    )

    target = select_primary_pma_target(
        repo_id="repo-1",
        bindings=bindings,
        repo_id_by_workspace={str(previous_workspace.resolve()): "repo-1"},
    )

    assert target is not None
    assert target.surface_key == surface_keys[1]
    assert target.workspace_root == str(previous_workspace)


def test_select_primary_pma_target_returns_none_without_repo_match(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    target = select_primary_pma_target(
        repo_id="repo-missing",
        bindings=(
            PmaChatSurfaceBinding(
                surface_key="discord-channel",
                workspace_path=str(workspace),
                repo_id="repo-1",
                is_primary_pma=True,
            ),
        ),
        repo_id_by_workspace={str(workspace.resolve()): "repo-1"},
    )

    assert target is None
