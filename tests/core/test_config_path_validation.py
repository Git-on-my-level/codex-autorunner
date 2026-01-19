from pathlib import Path

import pytest

from codex_autorunner.core.config import ConfigError, ConfigPathError, load_repo_config
from codex_autorunner.core.path_utils import resolve_config_path


class TestDocsPathValidation:
    """Test docs.* path validation in repo config."""

    def test_valid_relative_docs_paths(self, tmp_path):
        """Valid relative docs paths are accepted."""
        repo_root = Path(tmp_path)
        config = {
            "version": 2,
            "mode": "repo",
            "docs": {
                "todo": ".codex-autorunner/TODO.md",
                "progress": ".codex-autorunner/PROGRESS.md",
                "opinions": ".codex-autorunner/OPINIONS.md",
                "spec": ".codex-autorunner/SPEC.md",
                "summary": ".codex-autorunner/SUMMARY.md",
            },
        }
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
docs:
  todo: .codex-autorunner/TODO.md
  progress: .codex-autorunner/PROGRESS.md
  opinions: .codex-autorunner/OPINIONS.md
  spec: .codex-autorunner/SPEC.md
  summary: .codex-autorunner/SUMMARY.md
 """
        )
        config = load_repo_config(repo_root)
        assert config.docs["todo"] == repo_root / ".codex-autorunner" / "TODO.md"

    def test_invalid_absolute_docs_path(self, tmp_path):
        """Absolute paths in docs are rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
docs:
  todo: /absolute/path/TODO.md
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "docs.todo" in str(exc_info.value)
        assert "absolute" in str(exc_info.value).lower()

    def test_invalid_dotdot_docs_path(self, tmp_path):
        """Paths with .. in docs are rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
docs:
  todo: ../external/TODO.md
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "docs.todo" in str(exc_info.value)
        assert ".." in str(exc_info.value)

    def test_invalid_home_expansion_docs_path(self, tmp_path):
        """Home expansion in docs is rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
docs:
  todo: ~/TODO.md
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "docs.todo" in str(exc_info.value)
        assert (
            "absolute" in str(exc_info.value).lower()
            or "not allowed" in str(exc_info.value).lower()
        )


class TestAppServerPathValidation:
    """Test app_server.state_root path validation."""

    def test_valid_relative_state_root(self, tmp_path):
        """Valid relative state_root is accepted."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
app_server:
  state_root: .codex-autorunner/workspaces
"""
        )
        config = load_repo_config(repo_root)
        assert (
            config.app_server.state_root
            == repo_root / ".codex-autorunner" / "workspaces"
        )

    def test_valid_home_expansion_state_root(self, tmp_path):
        """Home expansion in state_root is accepted."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
app_server:
  state_root: ~/.codex-autorunner/workspaces
"""
        )
        config = load_repo_config(repo_root)
        assert config.app_server.state_root.name == "workspaces"
        assert config.app_server.state_root.is_absolute()

    def test_invalid_absolute_state_root(self, tmp_path):
        """Absolute state_root is rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
app_server:
  state_root: /absolute/path
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "app_server.state_root" in str(exc_info.value)
        assert "absolute" in str(exc_info.value).lower()

    def test_invalid_dotdot_state_root(self, tmp_path):
        """State_root with .. is rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
app_server:
  state_root: ../external
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "app_server.state_root" in str(exc_info.value)
        assert ".." in str(exc_info.value)


class TestStaticAssetsPathValidation:
    """Test static_assets.cache_root path validation."""

    def test_valid_relative_cache_root(self, tmp_path):
        """Valid relative cache_root is accepted."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
static_assets:
  cache_root: .codex-autorunner/static-cache
"""
        )
        config = load_repo_config(repo_root)
        assert (
            config.static_assets.cache_root
            == repo_root / ".codex-autorunner" / "static-cache"
        )

    def test_valid_home_expansion_cache_root(self, tmp_path):
        """Home expansion in cache_root is accepted."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
static_assets:
  cache_root: ~/.codex-autorunner/static-cache
"""
        )
        config = load_repo_config(repo_root)
        assert config.static_assets.cache_root.name == "static-cache"
        assert config.static_assets.cache_root.is_absolute()


class TestHousekeepingPathValidation:
    """Test housekeeping.rules.*.path validation."""

    def test_valid_relative_housekeeping_path(self, tmp_path):
        """Valid relative housekeeping paths are accepted."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
housekeeping:
  rules:
    - name: test
      kind: directory
      path: .codex-autorunner/runs
"""
        )
        config = load_repo_config(repo_root)
        assert "test" in [rule.name for rule in config.housekeeping.rules]

    def test_valid_home_expansion_housekeeping_path(self, tmp_path):
        """Home expansion in housekeeping paths is accepted."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
housekeeping:
  rules:
    - name: update_cache
      kind: directory
      path: ~/.codex-autorunner/update_cache
"""
        )
        config = load_repo_config(repo_root)
        update_cache_rule = next(
            (r for r in config.housekeeping.rules if r.name == "update_cache"), None
        )
        assert update_cache_rule is not None

    def test_invalid_absolute_housekeeping_path(self, tmp_path):
        """Absolute housekeeping paths are rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
housekeeping:
  rules:
    - name: test
      kind: directory
      path: /absolute/path
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "housekeeping.rules[0].path" in str(exc_info.value)
        assert "absolute" in str(exc_info.value).lower()

    def test_invalid_dotdot_housekeeping_path(self, tmp_path):
        """Housekeeping paths with .. are rejected."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
housekeeping:
  rules:
    - name: test
      kind: directory
      path: ../external
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "housekeeping.rules[0].path" in str(exc_info.value)
        assert ".." in str(exc_info.value)

    def test_valid_dotdot_in_home_expansion_housekeeping(self, tmp_path):
        """.. segments in home expansion are rejected for housekeeping."""
        repo_root = Path(tmp_path)
        config_path = repo_root / ".codex-autorunner" / "config.yml"
        config_path.write_text(
            """
version: 2
mode: repo
housekeeping:
  rules:
    - name: test
      kind: directory
      path: ~/../external
"""
        )
        with pytest.raises(ConfigError) as exc_info:
            load_repo_config(repo_root)
        assert "housekeeping.rules[0].path" in str(exc_info.value)


class TestPathResolutionEdgeCases:
    """Test edge cases in path resolution."""

    def test_empty_path_rejected(self, tmp_path):
        """Empty paths are rejected."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError):
            resolve_config_path("", repo_root)

    def test_whitespace_path_rejected(self, tmp_path):
        """Whitespace paths are rejected."""
        repo_root = Path(tmp_path)
        with pytest.raises(ConfigPathError):
            resolve_config_path("   ", repo_root)

    def test_trailing_slash_normalized(self, tmp_path):
        """Trailing slashes are normalized."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("test/", repo_root)
        assert path == repo_root / "test"

    def test_consecutive_slashes_normalized(self, tmp_path):
        """Consecutive slashes are normalized."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("test//file.txt", repo_root)
        assert path == repo_root / "test" / "file.txt"

    def test_dot_segments_allowed(self, tmp_path):
        """Current directory segments are preserved."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("./test.txt", repo_root)
        assert path == repo_root / "test.txt"

    def test_path_object_input(self, tmp_path):
        """Path objects are handled correctly."""
        repo_root = Path(tmp_path)
        path = resolve_config_path(Path("test"), repo_root)
        assert path == repo_root / "test"

    def test_resolved_path_includes_repo_root(self, tmp_path):
        """Resolved paths include repo root."""
        repo_root = Path(tmp_path)
        path = resolve_config_path("subdir/file.txt", repo_root)
        assert path.is_relative_to(repo_root)
        assert str(path).startswith(str(repo_root))
