"""Tests for state_roots module - the path authority for canonical state roots."""

from pathlib import Path

import pytest

from codex_autorunner.core.state_roots import (
    GLOBAL_STATE_ROOT_ENV,
    REPO_STATE_DIR,
    StateRootError,
    get_canonical_roots,
    is_within_allowed_root,
    resolve_cache_root,
    resolve_global_state_root,
    resolve_hub_state_root,
    resolve_hub_templates_root,
    resolve_repo_state_root,
    validate_path_within_roots,
)


class TestResolveRepoStateRoot:
    def test_returns_repo_codex_dir(self, tmp_path):
        result = resolve_repo_state_root(Path(tmp_path))
        assert result == Path(tmp_path) / REPO_STATE_DIR

    def test_does_not_create_dir(self, tmp_path):
        result = resolve_repo_state_root(Path(tmp_path))
        assert not result.exists()


class TestResolveGlobalStateRoot:
    def test_defaults_to_home_codex_autorunner(self, tmp_path, monkeypatch):
        monkeypatch.delenv(GLOBAL_STATE_ROOT_ENV, raising=False)
        result = resolve_global_state_root(repo_root=Path(tmp_path))
        assert result == Path.home() / REPO_STATE_DIR

    def test_respects_env_override(self, tmp_path, monkeypatch):
        custom_root = tmp_path / "custom_global"
        monkeypatch.setenv(GLOBAL_STATE_ROOT_ENV, str(custom_root))
        result = resolve_global_state_root(repo_root=Path(tmp_path))
        assert result == custom_root.resolve()

    def test_respects_home_expansion_in_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv(GLOBAL_STATE_ROOT_ENV, "~/.custom_car_state")
        result = resolve_global_state_root(repo_root=Path(tmp_path))
        assert result == Path.home() / ".custom_car_state"


class TestResolveHubStateRoot:
    def test_returns_hub_codex_dir(self, tmp_path):
        result = resolve_hub_state_root(Path(tmp_path))
        assert result == Path(tmp_path) / REPO_STATE_DIR

    def test_hub_state_includes_templates(self, tmp_path):
        templates = resolve_hub_templates_root(Path(tmp_path))
        assert templates == Path(tmp_path) / REPO_STATE_DIR / "templates"


class TestResolveCacheRoot:
    def test_uses_tmpdir_env(self, monkeypatch):
        custom_tmp = "/custom/tmp"
        monkeypatch.setenv("TMPDIR", custom_tmp)
        result = resolve_cache_root()
        assert result == Path(custom_tmp)

    def test_defaults_to_tmp(self, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        result = resolve_cache_root()
        assert result == Path("/tmp")


class TestIsWithinAllowedRoot:
    def test_within_root(self, tmp_path):
        allowed = [Path(tmp_path)]
        child = Path(tmp_path) / "subdir" / "file.txt"
        assert is_within_allowed_root(child, allowed_roots=allowed) is True

    def test_outside_root(self, tmp_path):
        allowed = [Path(tmp_path)]
        outside = Path("/etc/passwd")
        assert is_within_allowed_root(outside, allowed_roots=allowed) is False

    def test_multiple_roots(self, tmp_path):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        allowed = [root1, root2]
        child1 = root1 / "file.txt"
        child2 = root2 / "file.txt"
        assert is_within_allowed_root(child1, allowed_roots=allowed) is True
        assert is_within_allowed_root(child2, allowed_roots=allowed) is True

    def test_exact_root_match(self, tmp_path):
        allowed = [Path(tmp_path)]
        assert is_within_allowed_root(Path(tmp_path), allowed_roots=allowed) is True

    def test_resolve_symlinks_by_default(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)
        allowed = [real_dir]
        child_via_link = link_dir / "file.txt"
        assert is_within_allowed_root(child_via_link, allowed_roots=allowed) is True

    def test_no_resolve_when_disabled(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)
        allowed = [real_dir]
        child_via_link = link_dir / "file.txt"
        assert (
            is_within_allowed_root(child_via_link, allowed_roots=allowed, resolve=False)
            is False
        )


class TestValidatePathWithinRoots:
    def test_validates_within_root(self, tmp_path):
        allowed = [Path(tmp_path)]
        child = Path(tmp_path) / "subdir" / "file.txt"
        assert validate_path_within_roots(child, allowed_roots=allowed) is True

    def test_raises_outside_root(self, tmp_path):
        allowed = [Path(tmp_path)]
        outside = Path("/etc/passwd")
        with pytest.raises(StateRootError) as exc_info:
            validate_path_within_roots(outside, allowed_roots=allowed)
        assert str(outside) in str(exc_info.value)
        assert exc_info.value.allowed_roots == allowed

    def test_error_includes_allowed_roots(self, tmp_path):
        allowed = [Path(tmp_path)]
        outside = Path("/etc/passwd")
        with pytest.raises(StateRootError) as exc_info:
            validate_path_within_roots(outside, allowed_roots=allowed)
        assert exc_info.value.path == outside
        assert exc_info.value.allowed_roots == allowed


class TestGetCanonicalRoots:
    def test_includes_global_root_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(GLOBAL_STATE_ROOT_ENV, raising=False)
        roots = get_canonical_roots()
        global_root = resolve_global_state_root()
        assert global_root in roots

    def test_includes_repo_root_when_provided(self, tmp_path):
        roots = get_canonical_roots(repo_root=Path(tmp_path))
        repo_state_root = resolve_repo_state_root(Path(tmp_path))
        assert repo_state_root in roots

    def test_includes_hub_root_when_provided(self, tmp_path):
        roots = get_canonical_roots(hub_root=Path(tmp_path))
        hub_state_root = resolve_hub_state_root(Path(tmp_path))
        assert hub_state_root in roots

    def test_all_roots_when_all_provided(self, tmp_path, monkeypatch):
        monkeypatch.delenv(GLOBAL_STATE_ROOT_ENV, raising=False)
        global_root = tmp_path / "global"
        roots = get_canonical_roots(
            repo_root=tmp_path / "repo",
            hub_root=tmp_path / "hub",
            global_root=global_root,
        )
        assert global_root in roots
        assert resolve_repo_state_root(tmp_path / "repo") in roots
        assert resolve_hub_state_root(tmp_path / "hub") in roots


class TestStateRootContract:
    def test_repo_state_paths_are_within_repo_root(self, tmp_path):
        repo_root = Path(tmp_path)
        state_root = resolve_repo_state_root(repo_root)
        canonical_paths = [
            state_root / "tickets",
            state_root / "contextspace",
            state_root / "config.yml",
            state_root / "state.sqlite3",
            state_root / "codex-autorunner.log",
            state_root / "lock",
            state_root / "runs",
            state_root / "flows.db",
        ]
        for path in canonical_paths:
            assert is_within_allowed_root(path, allowed_roots=[state_root])

    def test_global_state_paths_are_within_global_root(self, tmp_path, monkeypatch):
        global_root = tmp_path / "global_state"
        monkeypatch.setenv(GLOBAL_STATE_ROOT_ENV, str(global_root))
        resolved_global = resolve_global_state_root(repo_root=tmp_path)
        canonical_paths = [
            resolved_global / "update_cache",
            resolved_global / "update_status.json",
            resolved_global / "locks",
            resolved_global / "workspaces",
        ]
        for path in canonical_paths:
            assert is_within_allowed_root(path, allowed_roots=[resolved_global])

    def test_cache_is_explicitly_outside_canonical(self, tmp_path, monkeypatch):
        monkeypatch.delenv(GLOBAL_STATE_ROOT_ENV, raising=False)
        global_root = resolve_global_state_root()
        cache_root = resolve_cache_root()
        assert is_within_allowed_root(cache_root, allowed_roots=[global_root]) is False
