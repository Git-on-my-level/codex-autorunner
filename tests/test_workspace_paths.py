import logging
from pathlib import Path

import pytest

from codex_autorunner.contextspace.paths import (
    normalize_contextspace_rel_path,
    write_contextspace_doc,
    write_contextspace_file,
)


def test_rejects_absolute_and_parent_refs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ValueError):
        normalize_contextspace_rel_path(repo_root, "/etc/passwd")
    with pytest.raises(ValueError):
        normalize_contextspace_rel_path(repo_root, "../secrets")


def test_allows_simple_relative_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".codex-autorunner" / "contextspace").mkdir(parents=True)

    abs_path, rel = normalize_contextspace_rel_path(repo_root, "notes/todo.md")
    assert (
        abs_path
        == repo_root / ".codex-autorunner" / "contextspace" / "notes" / "todo.md"
    )
    assert rel == "notes/todo.md"


def test_blocks_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    contextspace = repo_root / ".codex-autorunner" / "contextspace"
    contextspace.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    escape = contextspace / "link"
    escape.symlink_to(outside)

    with pytest.raises(ValueError):
        normalize_contextspace_rel_path(repo_root, "link/secret.txt")


def test_write_contextspace_file_logs_draft_invalidation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _raise(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "codex_autorunner.contextspace.paths.draft_utils.invalidate_drafts_for_path",
        _raise,
    )

    with caplog.at_level(logging.WARNING, logger="codex_autorunner.contextspace.paths"):
        content = write_contextspace_file(repo_root, "notes/todo.md", "hello")

    assert content == "hello"
    assert "contextspace.draft_invalidation_failed" in caplog.text


def test_write_contextspace_doc_logs_draft_invalidation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _raise(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "codex_autorunner.contextspace.paths.draft_utils.invalidate_drafts_for_path",
        _raise,
    )

    with caplog.at_level(logging.WARNING, logger="codex_autorunner.contextspace.paths"):
        content = write_contextspace_doc(repo_root, "spec", "spec data")

    assert content == "spec data"
    assert "contextspace.draft_invalidation_failed" in caplog.text
