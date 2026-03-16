import logging
from pathlib import Path

import pytest

from codex_autorunner.contextspace.paths import (
    contextspace_doc_path,
    normalize_contextspace_doc_kind,
    write_contextspace_doc,
)


def test_normalize_contextspace_doc_kind_accepts_only_canonical_docs() -> None:
    assert normalize_contextspace_doc_kind("active_context") == "active_context"
    assert normalize_contextspace_doc_kind("decisions") == "decisions"
    assert normalize_contextspace_doc_kind("spec") == "spec"

    with pytest.raises(ValueError):
        normalize_contextspace_doc_kind("notes")


def test_contextspace_doc_path_uses_canonical_location(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    assert contextspace_doc_path(repo_root, "active_context") == (
        repo_root / ".codex-autorunner" / "contextspace" / "active_context.md"
    )


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
