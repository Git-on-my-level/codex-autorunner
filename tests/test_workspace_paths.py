import logging
from pathlib import Path

import pytest

from codex_autorunner.workspace.paths import (
    normalize_workspace_rel_path,
    write_workspace_doc,
    write_workspace_file,
)


def test_rejects_absolute_and_parent_refs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ValueError):
        normalize_workspace_rel_path(repo_root, "/etc/passwd")
    with pytest.raises(ValueError):
        normalize_workspace_rel_path(repo_root, "../secrets")


def test_allows_simple_relative_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".codex-autorunner" / "workspace").mkdir(parents=True)

    abs_path, rel = normalize_workspace_rel_path(repo_root, "notes/todo.md")
    assert (
        abs_path == repo_root / ".codex-autorunner" / "workspace" / "notes" / "todo.md"
    )
    assert rel == "notes/todo.md"


def test_blocks_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    workspace = repo_root / ".codex-autorunner" / "workspace"
    workspace.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workspace / "link"
    escape.symlink_to(outside)

    with pytest.raises(ValueError):
        normalize_workspace_rel_path(repo_root, "link/secret.txt")


def test_write_workspace_file_logs_draft_invalidation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _raise(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "codex_autorunner.workspace.paths.draft_utils.invalidate_drafts_for_path",
        _raise,
    )

    with caplog.at_level(logging.WARNING, logger="codex_autorunner.workspace.paths"):
        content = write_workspace_file(repo_root, "notes/todo.md", "hello")

    assert content == "hello"
    assert "workspace.draft_invalidation_failed" in caplog.text


def test_write_workspace_doc_logs_draft_invalidation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _raise(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "codex_autorunner.workspace.paths.draft_utils.invalidate_drafts_for_path",
        _raise,
    )

    with caplog.at_level(logging.WARNING, logger="codex_autorunner.workspace.paths"):
        content = write_workspace_doc(repo_root, "spec", "spec data")

    assert content == "spec data"
    assert "workspace.draft_invalidation_failed" in caplog.text
