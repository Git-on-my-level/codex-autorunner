from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.integrations.discord import service as discord_service_module


def test_resolve_base_repo_id_prefers_worktree_of_metadata() -> None:
    repo_entry = SimpleNamespace(
        kind="worktree",
        id="base-repo--thread-chat-123",
        worktree_of="base-repo",
    )

    assert discord_service_module._resolve_base_repo_id(repo_entry) == "base-repo"


def test_resolve_base_repo_id_infers_from_worktree_repo_id() -> None:
    repo_entry = SimpleNamespace(
        kind="worktree",
        id="base-repo--thread-chat-123",
        worktree_of=None,
    )

    assert discord_service_module._resolve_base_repo_id(repo_entry) == "base-repo"


def test_resolve_base_repo_id_prefers_longest_manifest_prefix_match() -> None:
    repo_entry = SimpleNamespace(
        kind="worktree",
        id="ml--infra--thread--chat-123",
        worktree_of=None,
    )
    manifest_repos = [
        SimpleNamespace(kind="base", id="ml"),
        SimpleNamespace(kind="base", id="ml--infra"),
    ]

    assert (
        discord_service_module._resolve_base_repo_id(
            repo_entry, manifest_repos=manifest_repos
        )
        == "ml--infra"
    )


def test_resolve_base_repo_id_infers_from_legacy_wt_repo_id() -> None:
    repo_entry = SimpleNamespace(
        kind="worktree",
        id="codex-autorunner-wt-1",
        worktree_of=None,
    )
    manifest_repos = [SimpleNamespace(kind="base", id="codex-autorunner")]

    assert (
        discord_service_module._resolve_base_repo_id(
            repo_entry, manifest_repos=manifest_repos
        )
        == "codex-autorunner"
    )


def test_resolve_base_repo_id_prefers_modern_separator_before_legacy_marker() -> None:
    repo_entry = SimpleNamespace(
        kind="worktree",
        id="alpha-wt-beta--thread-1",
        worktree_of=None,
    )

    assert (
        discord_service_module._resolve_base_repo_id(repo_entry, manifest_repos=[])
        == "alpha-wt-beta"
    )


def test_resolve_valid_base_repo_id_uses_git_common_dir_fallback(
    tmp_path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = hub_root / "worktrees" / "discord-car-wt-2"
    workspace_root.mkdir(parents=True)
    base_repo_root = hub_root / "repos" / "codex-autorunner"
    (base_repo_root / ".git").mkdir(parents=True)
    common_git_dir = str((base_repo_root / ".git").resolve())

    def _fake_run_git(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{common_git_dir}\n")

    monkeypatch.setattr(discord_service_module, "run_git", _fake_run_git)

    repo_entry = SimpleNamespace(
        kind="worktree",
        id="discord-car-wt-2",
        worktree_of=None,
    )
    manifest_repos = [
        SimpleNamespace(
            kind="base",
            id="codex-autorunner",
            path=Path("repos/codex-autorunner"),
        ),
        SimpleNamespace(
            kind="worktree",
            id="discord-car-wt-2",
            path=Path("worktrees/discord-car-wt-2"),
        ),
    ]

    assert (
        discord_service_module._resolve_valid_base_repo_id(
            repo_entry,
            manifest_repos=manifest_repos,
            workspace_root=workspace_root,
            hub_root=hub_root,
        )
        == "codex-autorunner"
    )


def test_resolve_valid_base_repo_id_keeps_valid_inferred_base(monkeypatch) -> None:
    repo_entry = SimpleNamespace(
        kind="worktree",
        id="base-repo--thread-chat-123",
        worktree_of=None,
    )
    manifest_repos = [
        SimpleNamespace(kind="base", id="base-repo", path=Path("repos/base-repo"))
    ]

    def _fail_run_git(*_args, **_kwargs):
        raise AssertionError("run_git should not be called when inferred base is valid")

    monkeypatch.setattr(discord_service_module, "run_git", _fail_run_git)

    assert (
        discord_service_module._resolve_valid_base_repo_id(
            repo_entry,
            manifest_repos=manifest_repos,
            workspace_root=Path("/tmp/worktree"),
            hub_root=Path("/tmp/hub"),
        )
        == "base-repo"
    )
