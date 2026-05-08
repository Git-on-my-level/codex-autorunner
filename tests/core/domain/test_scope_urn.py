from __future__ import annotations

import pytest

from codex_autorunner.core.domain.scope_urn import (
    VALID_SCOPE_KINDS,
    ScopeUrnError,
    ScopeUrnKindError,
    ScopeUrnParseError,
    format_scope_urn,
    parse_scope_urn,
)


class TestFormatScopeUrn:
    def test_hub(self) -> None:
        assert format_scope_urn(kind="hub") == "hub"

    def test_repo(self) -> None:
        assert format_scope_urn(kind="repo", id="abc") == "repo:abc"

    def test_worktree(self) -> None:
        assert (
            format_scope_urn(kind="worktree", id="wt1", parent_repo_id="repo1")
            == "worktree:repo1/wt1"
        )

    def test_filesystem(self) -> None:
        assert (
            format_scope_urn(kind="filesystem", id="/tmp/car repo")
            == "filesystem:%2Ftmp%2Fcar%20repo"
        )

    def test_unknown_kind_raises_kind_error(self) -> None:
        with pytest.raises(ScopeUrnKindError, match="Unknown scope kind"):
            format_scope_urn(kind="bogus", id="x")

    def test_repo_missing_id_raises(self) -> None:
        with pytest.raises(ScopeUrnError, match="requires an id"):
            format_scope_urn(kind="repo")

    def test_worktree_missing_id_raises(self) -> None:
        with pytest.raises(ScopeUrnError, match="requires an id"):
            format_scope_urn(kind="worktree", parent_repo_id="r1")

    def test_worktree_missing_parent_raises(self) -> None:
        with pytest.raises(ScopeUrnError, match="parent_repo_id"):
            format_scope_urn(kind="worktree", id="wt1")

    def test_worktree_parent_with_slash_raises(self) -> None:
        with pytest.raises(ScopeUrnError, match="must not contain '/'"):
            format_scope_urn(kind="worktree", id="wt1", parent_repo_id="owner/repo")

    def test_worktree_id_with_slash_raises(self) -> None:
        with pytest.raises(ScopeUrnError, match="must not contain '/'"):
            format_scope_urn(kind="worktree", id="branch/wt1", parent_repo_id="r1")

    def test_filesystem_missing_path_raises(self) -> None:
        with pytest.raises(ScopeUrnError, match="requires a path"):
            format_scope_urn(kind="filesystem")


class TestParseScopeUrn:
    def test_hub(self) -> None:
        result = parse_scope_urn("hub")
        assert result == {"kind": "hub", "id": None, "parent_repo_id": None}

    def test_repo(self) -> None:
        result = parse_scope_urn("repo:abc")
        assert result == {"kind": "repo", "id": "abc", "parent_repo_id": None}

    def test_worktree(self) -> None:
        result = parse_scope_urn("worktree:repo1/wt1")
        assert result == {"kind": "worktree", "id": "wt1", "parent_repo_id": "repo1"}

    def test_filesystem(self) -> None:
        result = parse_scope_urn("filesystem:%2Ftmp%2Fcar%20repo")
        assert result == {
            "kind": "filesystem",
            "id": "/tmp/car repo",
            "parent_repo_id": None,
        }

    def test_empty_string_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="non-empty string"):
            parse_scope_urn("")

    def test_none_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError):
            parse_scope_urn(None)  # type: ignore[arg-type]

    def test_no_colon_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="':' separator"):
            parse_scope_urn("reposomething")

    def test_unknown_kind_raises_kind_error(self) -> None:
        with pytest.raises(ScopeUrnKindError, match="Unknown scope kind"):
            parse_scope_urn("planet:earth")

    def test_hub_with_path_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="path component"):
            parse_scope_urn("hub:extra")

    def test_repo_empty_id_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="requires an id"):
            parse_scope_urn("repo:")

    def test_repo_with_slash_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="must not contain '/'"):
            parse_scope_urn("repo:a/b")

    def test_worktree_no_slash_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="<repo_id>/<worktree_id>"):
            parse_scope_urn("worktree:noslash")

    def test_worktree_leading_slash_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="<repo_id>/<worktree_id>"):
            parse_scope_urn("worktree:/wt1")

    def test_worktree_trailing_slash_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="<repo_id>/<worktree_id>"):
            parse_scope_urn("worktree:repo1/")

    def test_filesystem_empty_path_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="requires a path"):
            parse_scope_urn("filesystem:")

    def test_filesystem_invalid_escape_raises_parse_error(self) -> None:
        with pytest.raises(ScopeUrnParseError, match="invalid escape"):
            parse_scope_urn("filesystem:%ZZ")


class TestUrnRoundTrips:
    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            ({"kind": "hub"}, "hub"),
            ({"kind": "repo", "id": "r1"}, "repo:r1"),
            (
                {"kind": "worktree", "id": "wt1", "parent_repo_id": "r1"},
                "worktree:r1/wt1",
            ),
            (
                {"kind": "filesystem", "id": "/tmp/car repo"},
                "filesystem:%2Ftmp%2Fcar%20repo",
            ),
        ],
    )
    def test_format_then_parse_round_trips(self, kwargs: dict, expected: str) -> None:
        urn = format_scope_urn(**kwargs)
        assert urn == expected
        result = parse_scope_urn(urn)
        assert result["kind"] == kwargs.get("kind")
        assert result["id"] == kwargs.get("id")
        assert result["parent_repo_id"] == kwargs.get("parent_repo_id")

    def test_valid_scope_kinds_includes_all(self) -> None:
        assert VALID_SCOPE_KINDS == {
            "hub",
            "repo",
            "worktree",
            "filesystem",
        }
