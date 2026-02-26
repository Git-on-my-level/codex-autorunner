from __future__ import annotations

from codex_autorunner.core.chat_bound_worktrees import (
    is_chat_bound_worktree_identity,
)


def test_chat_bound_identity_by_managed_path() -> None:
    assert (
        is_chat_bound_worktree_identity(
            branch=None,
            repo_id="codex-autorunner--discord-1",
            source_path="worktrees/chat-app-managed/discord/discord-1",
        )
        is True
    )


def test_chat_bound_identity_by_repo_id_suffix() -> None:
    assert (
        is_chat_bound_worktree_identity(
            branch="feature/something",
            repo_id="codex-autorunner--tg-3",
            source_path="worktrees/codex-autorunner--tg-3",
        )
        is True
    )


def test_chat_bound_identity_by_known_thread_branch_formats() -> None:
    assert (
        is_chat_bound_worktree_identity(
            branch="thread-1475157287927812277",
            repo_id="base--thread-1475157287927812277",
            source_path="worktrees/base--thread-1475157287927812277",
        )
        is True
    )
    assert (
        is_chat_bound_worktree_identity(
            branch="thread-chat-1001234567890-msg-15-upd-200",
            repo_id="base--thread-chat-1001234567890-msg-15-upd-200",
            source_path="worktrees/base--thread-chat-1001234567890-msg-15-upd-200",
        )
        is True
    )


def test_chat_bound_identity_false_for_regular_worktree() -> None:
    assert (
        is_chat_bound_worktree_identity(
            branch="feature/refactor",
            repo_id="codex-autorunner--feature-refactor",
            source_path="worktrees/codex-autorunner--feature-refactor",
        )
        is False
    )
