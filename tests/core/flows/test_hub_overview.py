from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows import hub_overview as hub_overview_module
from codex_autorunner.core.flows.hub_overview import build_hub_flow_overview_entries
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.manifest import Manifest, ManifestRepo


def _manifest_with_worktrees() -> Manifest:
    return Manifest(
        version=2,
        repos=[
            ManifestRepo(
                id="base",
                path=Path("repos/base"),
                enabled=True,
                auto_run=False,
                kind="base",
            ),
            ManifestRepo(
                id="base--wt-visible",
                path=Path("worktrees/base--wt-visible"),
                enabled=True,
                auto_run=False,
                kind="worktree",
                worktree_of="base",
            ),
            ManifestRepo(
                id="base--wt-hidden",
                path=Path("worktrees/base--wt-hidden"),
                enabled=True,
                auto_run=False,
                kind="worktree",
                worktree_of="base",
            ),
        ],
    )


def _create_run(repo_root: Path, run_id: str, *, status: FlowRunStatus) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, status)


def test_build_hub_overview_filters_inactive_manifest_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_with_worktrees()
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {"base--wt-visible": 1},
    )

    entries = build_hub_flow_overview_entries(
        hub_root=tmp_path,
        manifest=manifest,
        raw_config={},
    )

    repo_ids = [entry.repo_id for entry in entries]
    assert repo_ids == ["base", "base--wt-visible"]


def test_build_hub_overview_includes_manifest_worktree_with_active_flow_without_chat_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_with_worktrees()
    active_repo_root = tmp_path / "worktrees" / "base--wt-hidden"
    _create_run(active_repo_root, "run-active", status=FlowRunStatus.RUNNING)
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {},
    )

    entries = build_hub_flow_overview_entries(
        hub_root=tmp_path,
        manifest=manifest,
        raw_config={},
    )

    repo_ids = [entry.repo_id for entry in entries]
    assert repo_ids == ["base", "base--wt-hidden"]


def test_build_hub_overview_includes_active_unregistered_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = Manifest(
        version=2,
        repos=[
            ManifestRepo(
                id="base",
                path=Path("repos/base"),
                enabled=True,
                auto_run=False,
                kind="base",
            ),
        ],
    )
    active_worktree = tmp_path / "worktrees" / "base--wt-live"
    (active_worktree / ".git").mkdir(parents=True)
    (active_worktree / ".codex-autorunner" / "flows").mkdir(parents=True)
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {"base--wt-live": 2},
    )

    entries = build_hub_flow_overview_entries(
        hub_root=tmp_path,
        manifest=manifest,
        raw_config={"hub": {"worktrees_root": "worktrees"}},
    )

    repo_ids = [entry.repo_id for entry in entries]
    assert repo_ids == ["base", "base--wt-live"]
    assert entries[-1].unregistered is True
    assert entries[-1].label == "wt-live (unregistered)"


def test_build_hub_overview_fail_open_when_binding_counts_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_with_worktrees()
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    entries = build_hub_flow_overview_entries(
        hub_root=tmp_path,
        manifest=manifest,
        raw_config={},
    )

    repo_ids = [entry.repo_id for entry in entries]
    assert repo_ids == ["base", "base--wt-visible", "base--wt-hidden"]


def test_base_repo_id_with_double_dash_is_not_treated_as_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = Manifest(
        version=2,
        repos=[
            ManifestRepo(
                id="base--name",
                path=Path("repos/base--name"),
                enabled=True,
                auto_run=False,
                kind="base",
            ),
            ManifestRepo(
                id="base--name--wt-1",
                path=Path("worktrees/base--name--wt-1"),
                enabled=True,
                auto_run=False,
                kind="worktree",
                worktree_of="base--name",
            ),
        ],
    )
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {},
    )

    entries = build_hub_flow_overview_entries(
        hub_root=tmp_path,
        manifest=manifest,
        raw_config={},
    )

    assert [entry.repo_id for entry in entries] == ["base--name"]
    assert entries[0].is_worktree is False
