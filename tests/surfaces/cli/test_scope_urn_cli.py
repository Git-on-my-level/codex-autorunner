from __future__ import annotations

import pytest

from codex_autorunner.surfaces.cli.pma_control_plane import (
    normalize_scope_urn_to_owner_fields,
)


def test_normalize_scope_urn_repo():
    rk, rid, wr = normalize_scope_urn_to_owner_fields("repo:my-repo")
    assert rk == "repo"
    assert rid == "my-repo"
    assert wr is None


def test_normalize_scope_urn_filesystem():
    rk, rid, wr = normalize_scope_urn_to_owner_fields("filesystem:/tmp/workspace")
    assert rk is None
    assert rid is None
    assert wr == "/tmp/workspace"


def test_normalize_scope_urn_worktree():
    rk, rid, wr = normalize_scope_urn_to_owner_fields("worktree:parent-repo/wt-1")
    assert rk == "worktree"
    assert rid == "wt-1"
    assert wr is None


def test_normalize_scope_urn_hub():
    rk, rid, wr = normalize_scope_urn_to_owner_fields("hub")
    assert rk is None
    assert rid is None
    assert wr is None


def test_normalize_scope_urn_none():
    rk, rid, wr = normalize_scope_urn_to_owner_fields(None)
    assert rk is None
    assert rid is None
    assert wr is None


def test_normalize_scope_urn_empty():
    rk, rid, wr = normalize_scope_urn_to_owner_fields("")
    assert rk is None


def test_normalize_scope_urn_invalid_exits():
    from click.exceptions import Exit

    with pytest.raises(Exit):
        normalize_scope_urn_to_owner_fields("invalid-kind:whatever")
