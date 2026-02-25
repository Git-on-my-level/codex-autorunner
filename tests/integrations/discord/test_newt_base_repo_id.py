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
