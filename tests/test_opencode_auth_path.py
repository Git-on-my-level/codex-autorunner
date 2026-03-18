"""Tests for canonical OpenCode auth path resolution."""

from pathlib import Path
from unittest.mock import patch

from codex_autorunner.core.utils import resolve_opencode_auth_path


class TestResolveOpenCodeAuthPath:
    def test_uses_xdg_data_home_when_set(self, tmp_path: Path, monkeypatch) -> None:
        xdg_data_home = tmp_path / "xdg-share"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
        monkeypatch.delenv("HOME", raising=False)

        result = resolve_opencode_auth_path()
        assert result == xdg_data_home / "opencode" / "auth.json"

    def test_uses_home_when_xdg_not_set(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "user-home"
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))

        result = resolve_opencode_auth_path()
        assert result == home / ".local" / "share" / "opencode" / "auth.json"

    def test_xdg_takes_precedence_over_home(self, tmp_path: Path, monkeypatch) -> None:
        xdg_data_home = tmp_path / "xdg-share"
        home = tmp_path / "user-home"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
        monkeypatch.setenv("HOME", str(home))

        result = resolve_opencode_auth_path()
        assert result == xdg_data_home / "opencode" / "auth.json"
        assert "user-home" not in str(result)

    def test_infers_from_workspace_when_no_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)

        workspace = Path("/Users/alice/project")
        with patch(
            "codex_autorunner.core.utils.infer_home_from_workspace",
            return_value=Path("/Users/alice"),
        ):
            result = resolve_opencode_auth_path(workspace)
            assert result is not None
            assert result == Path("/Users/alice/.local/share/opencode/auth.json")

    def test_returns_none_when_no_home_or_workspace(self, monkeypatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)

        result = resolve_opencode_auth_path()
        assert result is None

    def test_uses_env_dict_when_provided(self, tmp_path: Path) -> None:
        custom_home = tmp_path / "custom-home"
        env = {"HOME": str(custom_home)}

        result = resolve_opencode_auth_path(env=env)
        assert result == custom_home / ".local" / "share" / "opencode" / "auth.json"

    def test_env_dict_xdg_takes_precedence(self, tmp_path: Path) -> None:
        xdg_data_home = tmp_path / "xdg"
        custom_home = tmp_path / "home"
        env = {"XDG_DATA_HOME": str(xdg_data_home), "HOME": str(custom_home)}

        result = resolve_opencode_auth_path(env=env)
        assert result == xdg_data_home / "opencode" / "auth.json"

    def test_workspace_fallback_when_no_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)

        workspace = Path("/home/bob/repo")
        with patch(
            "codex_autorunner.core.utils.infer_home_from_workspace",
            return_value=Path("/home/bob"),
        ):
            result = resolve_opencode_auth_path(workspace)
            assert result is not None
            assert result == Path("/home/bob/.local/share/opencode/auth.json")

    def test_env_dict_overrides_workspace_inference(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)

        workspace = Path("/home/charlie/repo")
        custom_home = tmp_path / "custom"
        env = {"HOME": str(custom_home)}

        result = resolve_opencode_auth_path(workspace, env=env)
        assert result == custom_home / ".local" / "share" / "opencode" / "auth.json"

    def test_handles_macos_system_volumes_path(self, monkeypatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)

        workspace = Path("/System/Volumes/Data/Users/dave/project")
        with patch(
            "codex_autorunner.core.utils.infer_home_from_workspace",
            return_value=Path("/System/Volumes/Data/Users/dave"),
        ):
            result = resolve_opencode_auth_path(workspace)
            assert result is not None
            assert "System/Volumes/Data/Users/dave" in str(result)
