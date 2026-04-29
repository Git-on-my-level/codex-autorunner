"""Tests for state_roots module - the path authority for canonical state roots."""

from pathlib import Path

import pytest

from codex_autorunner.core.state_roots import (
    DISCORD_STATE_DB_FILENAME,
    GITHUB_BROKER_DB_FILENAME,
    GLOBAL_STATE_ROOT_ENV,
    HUB_MANIFEST_FILENAME,
    LIFECYCLE_EVENTS_DB_FILENAME,
    LIFECYCLE_EVENTS_FILENAME,
    ORCHESTRATION_DB_FILENAME,
    PMA_THREADS_DB_FILENAME,
    REPO_STATE_DIR,
    RUNNER_STATE_DB_FILENAME,
    TELEGRAM_STATE_DB_FILENAME,
    StateRootError,
    get_canonical_roots,
    is_within_allowed_root,
    resolve_cache_root,
    resolve_discord_state_path,
    resolve_global_github_broker_db_path,
    resolve_global_state_root,
    resolve_hub_agent_workspace_root,
    resolve_hub_apps_root,
    resolve_hub_lifecycle_events_db_path,
    resolve_hub_lifecycle_events_path,
    resolve_hub_manifest_path,
    resolve_hub_orchestration_db_path,
    resolve_hub_pma_threads_db_path,
    resolve_hub_runtime_root,
    resolve_hub_runtimes_root,
    resolve_hub_state_root,
    resolve_hub_templates_root,
    resolve_repo_flows_db_path,
    resolve_repo_runner_state_db_path,
    resolve_repo_state_root,
    resolve_telegram_state_path,
    validate_path_within_roots,
)
from codex_autorunner.integrations.chat.run_mirror import ChatRunMirror


class TestResolveRepoStateRoot:
    def test_returns_repo_codex_dir(self, tmp_path):
        result = resolve_repo_state_root(Path(tmp_path))
        assert result == Path(tmp_path) / REPO_STATE_DIR

    def test_does_not_create_dir(self, tmp_path):
        result = resolve_repo_state_root(Path(tmp_path))
        assert not result.exists()

    def test_repo_sqlite_paths_live_under_repo_state_root(self, tmp_path):
        repo_root = Path(tmp_path)
        state_root = resolve_repo_state_root(repo_root)
        assert resolve_repo_flows_db_path(repo_root) == state_root / "flows.db"
        assert (
            resolve_repo_runner_state_db_path(repo_root)
            == state_root / RUNNER_STATE_DB_FILENAME
        )


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

    def test_global_github_broker_path_uses_global_state_root(
        self, tmp_path, monkeypatch
    ):
        custom_root = tmp_path / "custom_global"
        monkeypatch.setenv(GLOBAL_STATE_ROOT_ENV, str(custom_root))
        assert resolve_global_github_broker_db_path(repo_root=tmp_path) == (
            custom_root / "github" / GITHUB_BROKER_DB_FILENAME
        )


class TestResolveHubStateRoot:
    def test_returns_hub_codex_dir(self, tmp_path):
        result = resolve_hub_state_root(Path(tmp_path))
        assert result == Path(tmp_path) / REPO_STATE_DIR

    def test_hub_state_includes_templates(self, tmp_path):
        templates = resolve_hub_templates_root(Path(tmp_path))
        assert templates == Path(tmp_path) / REPO_STATE_DIR / "templates"

    def test_hub_state_includes_apps(self, tmp_path):
        apps = resolve_hub_apps_root(Path(tmp_path))
        assert apps == Path(tmp_path) / REPO_STATE_DIR / "apps"

    def test_hub_state_includes_manifest_and_legacy_state_paths(self, tmp_path):
        hub_root = Path(tmp_path)
        hub_state = resolve_hub_state_root(hub_root)
        assert resolve_hub_manifest_path(hub_root) == hub_state / HUB_MANIFEST_FILENAME
        assert (
            resolve_hub_lifecycle_events_path(hub_root)
            == hub_state / LIFECYCLE_EVENTS_FILENAME
        )
        assert (
            resolve_hub_lifecycle_events_db_path(hub_root)
            == hub_state / LIFECYCLE_EVENTS_DB_FILENAME
        )
        assert (
            resolve_hub_pma_threads_db_path(hub_root)
            == hub_state / "pma" / PMA_THREADS_DB_FILENAME
        )

    def test_hub_state_includes_runtime_roots(self, tmp_path):
        runtimes = resolve_hub_runtimes_root(Path(tmp_path))
        runtime = resolve_hub_runtime_root(Path(tmp_path), runtime="zeroclaw")
        workspace = resolve_hub_agent_workspace_root(
            Path(tmp_path), runtime="zeroclaw", workspace_id="main"
        )
        assert runtimes == Path(tmp_path) / REPO_STATE_DIR / "runtimes"
        assert runtime == runtimes / "zeroclaw"
        assert workspace == runtimes / "zeroclaw" / "main"

    def test_agent_workspace_root_rejects_unsafe_segments(self, tmp_path):
        with pytest.raises(StateRootError):
            resolve_hub_runtime_root(Path(tmp_path), runtime="../escape")
        with pytest.raises(StateRootError):
            resolve_hub_agent_workspace_root(
                Path(tmp_path), runtime="zeroclaw", workspace_id="../escape"
            )


class TestResolveHubOrchestrationDbPath:
    def test_returns_orchestration_sqlite_under_hub_state(self, tmp_path):
        result = resolve_hub_orchestration_db_path(Path(tmp_path))
        expected = Path(tmp_path) / REPO_STATE_DIR / ORCHESTRATION_DB_FILENAME
        assert result == expected

    def test_orchestration_db_is_within_hub_state_root(self, tmp_path):
        result = resolve_hub_orchestration_db_path(Path(tmp_path))
        hub_state = resolve_hub_state_root(Path(tmp_path))
        assert is_within_allowed_root(result, allowed_roots=[hub_state])

    def test_orchestration_db_is_within_canonical_roots(self, tmp_path):
        result = resolve_hub_orchestration_db_path(Path(tmp_path))
        canonical = get_canonical_roots(hub_root=Path(tmp_path))
        assert is_within_allowed_root(result, allowed_roots=canonical)


class TestConfiguredChatStatePaths:
    def test_default_chat_state_paths_use_hub_state_root(self, tmp_path):
        hub_root = Path(tmp_path)
        hub_state = resolve_hub_state_root(hub_root)
        assert resolve_discord_state_path(hub_root, {}) == (
            hub_state / DISCORD_STATE_DB_FILENAME
        )
        assert resolve_telegram_state_path(hub_root, {}) == (
            hub_state / TELEGRAM_STATE_DB_FILENAME
        )

    def test_chat_state_paths_honor_config_override(self, tmp_path):
        hub_root = Path(tmp_path)
        raw_config = {
            "discord_bot": {"state_file": "custom/discord.db"},
            "telegram_bot": {"state_file": "/tmp/telegram.db"},
        }
        assert (
            resolve_discord_state_path(hub_root, raw_config)
            == (hub_root / "custom" / "discord.db").resolve()
        )
        assert (
            resolve_telegram_state_path(hub_root, raw_config)
            == Path("/tmp/telegram.db").resolve()
        )


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
            state_root / "pma" / "deliveries.jsonl",
            state_root / "chat" / "channel_directory.json",
            state_root / "flows" / "run-id" / "chat" / "inbound.jsonl",
            state_root / "flows" / "run-id" / "chat" / "outbound.jsonl",
            state_root / "discord_state.sqlite3",
            state_root / "telegram_state.sqlite3",
        ]
        for path in canonical_paths:
            assert is_within_allowed_root(path, allowed_roots=[state_root])

    def test_hub_state_paths_include_orchestration_db(self, tmp_path):
        hub_root = Path(tmp_path)
        hub_state = resolve_hub_state_root(hub_root)
        orchestration_db = resolve_hub_orchestration_db_path(hub_root)
        assert is_within_allowed_root(orchestration_db, allowed_roots=[hub_state])

    def test_hub_runtime_paths_are_within_hub_state_root(self, tmp_path):
        hub_root = Path(tmp_path)
        hub_state = resolve_hub_state_root(hub_root)
        workspace_root = resolve_hub_agent_workspace_root(
            hub_root, runtime="zeroclaw", workspace_id="main"
        )
        assert is_within_allowed_root(workspace_root, allowed_roots=[hub_state])

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

    def test_chat_run_mirror_rejects_paths_outside_repo_state_root(self, tmp_path):
        mirror = ChatRunMirror(Path(tmp_path))
        escaped_run_id = "../../../../outside-state-root"
        escaped_target = (
            Path(tmp_path)
            / ".codex-autorunner"
            / "flows"
            / escaped_run_id
            / "chat"
            / "inbound.jsonl"
        ).resolve()
        state_root = resolve_repo_state_root(Path(tmp_path)).resolve()
        assert (
            is_within_allowed_root(escaped_target, allowed_roots=[state_root]) is False
        )

        mirror.mirror_inbound(
            run_id=escaped_run_id,
            platform="telegram",
            event_type="flow_resume_command",
            text="/flow resume",
            chat_id=-1001,
            thread_id=1,
        )

        assert not escaped_target.exists()
