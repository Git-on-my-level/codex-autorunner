from pathlib import Path

import pytest

from codex_autorunner.core.path_utils import ConfigPathError, resolve_config_path


class TestResolveConfigPath:
    """Test cases for resolve_config_path function."""

    def test_resolve_relative_path(self, tmp_path):
        """Relative paths resolved to repo root."""
        repo_root = Path(tmp_path)
        path = resolve_config_path(".codex-autorunner/TODO.md", repo_root)
        assert path == repo_root / ".codex-autorunner" / "TODO.md"
        assert path.is_absolute()

    def test_resolve_nested_relative_path(self, tmp_path):
        """Nested relative paths resolved correctly."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("docs/nested/file.md", repo_root)
        assert path == repo_root / "docs" / "nested" / "file.md"

    def test_resolve_path_object(self, tmp_path):
        """Path objects are handled correctly."""
        repo_root = Path(tmp_path)
        path = resolve_config_path(Path("test.txt"), repo_root)
        assert path == repo_root / "test.txt"

    def test_resolve_home_expansion(self, tmp_path):
        """~ expanded to home directory."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("~/.codex/workspaces", repo_root, allow_home=True)
        assert path.name == "workspaces"
        assert str(path).endswith(".codex/workspaces")
        assert path.is_absolute()

    def test_resolve_home_expansion_simple(self, tmp_path):
        """Simple ~ expansion works."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("~/myfile.txt", repo_root, allow_home=True)
        assert path.name == "myfile.txt"
        assert path.is_absolute()

    def test_resolve_with_trailing_slash(self, tmp_path):
        """Paths with trailing slash are handled."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("test/", repo_root)
        assert path == repo_root / "test"

    def test_resolve_with_dot_segments(self, tmp_path):
        """Paths with '.' segments are handled."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("./test.txt", repo_root)
        assert path == repo_root / "test.txt"

    def test_reject_empty_path(self, tmp_path):
        """Empty paths are rejected."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("", repo_root)
        assert "empty" in str(exc_info.value).lower()

    def test_reject_whitespace_only_path(self, tmp_path):
        """Whitespace-only paths are rejected."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("   ", repo_root)
        assert "whitespace" in str(exc_info.value).lower()

    def test_reject_dotdot_segments(self, tmp_path):
        """.. segments rejected by default."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("../config.yml", repo_root)
        assert ".." in str(exc_info.value)
        assert "path" in str(exc_info.value).lower()

    def test_reject_dotdot_in_middle(self, tmp_path):
        """.. segments in the middle of path are rejected."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("docs/../config.yml", repo_root)
        assert ".." in str(exc_info.value)

    def test_reject_absolute_path(self, tmp_path):
        """Absolute paths rejected by default."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("/etc/config.yml", repo_root)
        assert "absolute" in str(exc_info.value).lower()

    def test_reject_home_expansion_when_not_allowed(self, tmp_path):
        """Home expansion rejected when allow_home=False."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("~/test.txt", repo_root, allow_home=False)
        assert "~" in str(exc_info.value) or "home" in str(exc_info.value).lower()

    def test_reject_path_outside_repo_root(self, tmp_path):
        """Paths resolving outside repo root are rejected."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("../../../etc/passwd", repo_root)
        assert "outside repo root" in str(exc_info.value).lower() or ".." in str(
            exc_info.value
        )

    def test_reject_dotdot_in_home_expansion(self, tmp_path):
        """.. segments rejected in home expansion."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("~/../external", repo_root, allow_home=True)
        assert ".." in str(exc_info.value)

    def test_allow_dotdot_segments(self, tmp_path):
        """.. segments allowed when allow_dotdot=True."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("../config.yml", repo_root, allow_dotdot=True)
        assert path.resolve().name == "config.yml"

    def test_allow_absolute_path(self, tmp_path):
        """Absolute paths allowed when allow_absolute=True."""
        repo_root = Path(tmp_path)
        absolute_path = Path("/absolute/path/to/file")
        path = resolve_config_path(str(absolute_path), repo_root, allow_absolute=True)
        assert path.resolve() == absolute_path.resolve()

    def test_scope_in_error_message(self, tmp_path):
        """Scope is included in error message."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("", repo_root, scope="docs.todo")
        assert "docs.todo" in str(exc_info.value)

    def test_resolved_path_in_error_message(self, tmp_path):
        """Resolved path is included in error message."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError) as exc_info:
            resolve_config_path("../test", repo_root)
        error_str = str(exc_info.value)
        assert "path:" in error_str or "resolved:" in error_str

    def test_normalizes_path(self, tmp_path):
        """Paths are normalized (remove redundant separators, etc.)."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("test//file.txt", repo_root)
        assert path == repo_root / "test" / "file.txt"

    def test_unicode_path(self, tmp_path):
        """Unicode paths are handled correctly."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("tëst/file.txt", repo_root)
        assert path == repo_root / "tëst" / "file.txt"

    def test_expands_environment_variables_not_supported(self, tmp_path):
        """Environment variables are NOT expanded (only ~ is supported)."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("$HOME/test.txt", repo_root)
        assert "$HOME" in str(path)
        assert path.name == "test.txt"


class TestConfigPathError:
    """Test cases for ConfigPathError exception."""

    def test_error_with_path_only(self):
        """Error message includes path."""
        exc = ConfigPathError("Test error", path="/path/to/file")
        assert "Test error" in str(exc)
        assert "/path/to/file" in str(exc)

    def test_error_with_resolved_path_only(self):
        """Error message includes resolved path."""
        resolved_path = Path("/resolved/path")
        exc = ConfigPathError("Test error", resolved=resolved_path)
        assert "Test error" in str(exc)
        assert str(resolved_path) in str(exc)

    def test_error_with_both_path_and_resolved(self):
        """Error message includes both path and resolved."""
        exc = ConfigPathError(
            "Test error",
            path="~/test",
            resolved=Path("/home/user/test"),
        )
        exc_str = str(exc)
        assert "Test error" in exc_str
        assert "~/test" in exc_str
        assert "/home/user/test" in exc_str

    def test_error_with_scope_only(self):
        """Error with scope only."""
        exc = ConfigPathError("Test error", scope="docs.todo")
        assert "Test error" in str(exc)
