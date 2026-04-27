from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tests.telegram_pma_workspace_support import _NewtHandler

from codex_autorunner.core.git_utils import GitError
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
)
from codex_autorunner.integrations.telegram.state_types import ThreadSummary


def _patch_newt_branch_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def _fake_reset(
        repo_root: Path,
        branch_name: str,
        *,
        repo_id_hint: Optional[str] = None,
        hub_client: Any = None,
    ) -> SimpleNamespace:
        calls.append({"repo_root": repo_root, "branch_name": branch_name})
        setup_command_count = 0
        if hub_client is not None:
            setup_result = await hub_client.run_workspace_setup_commands(
                SimpleNamespace(
                    workspace_root=str(repo_root),
                    repo_id_hint=repo_id_hint,
                )
            )
            setup_command_count = setup_result.setup_command_count
        return SimpleNamespace(
            default_branch="master",
            setup_command_count=setup_command_count,
            submodule_paths=(),
        )

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.commands.workspace_session_commands.run_newt_branch_reset",
        _fake_reset,
    )

    async def _fake_sync_binding(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.commands.execution._sync_telegram_thread_binding",
        _fake_sync_binding,
    )

    return calls


@pytest.mark.anyio
async def test_newt_branch_name_includes_chat_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch


@pytest.mark.anyio
async def test_newt_dirty_worktree_reports_blockers_with_submodule_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=tmp_path / "hub")

    async def _fail_reset(
        _repo_root: Path,
        _branch_name: str,
        *,
        repo_id_hint: Optional[str] = None,
        hub_client: Any = None,
    ) -> None:
        _ = repo_id_hint, hub_client
        raise GitError(
            "working tree has uncommitted changes; commit or stash before /newt"
        )

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.commands.workspace_session_commands.run_newt_branch_reset",
        _fail_reset,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.commands.workspace_session_commands.describe_newt_reject_state",
        lambda _repo_root: SimpleNamespace(
            reasons=(
                "1 submodule change, including `vendor/sdk`",
                "1 unstaged tracked change, including `changed.txt`",
            ),
            submodule_paths=("vendor/sdk",),
        ),
    )

    await handler._handle_newt(
        TelegramMessage(
            update_id=100,
            message_id=200,
            chat_id=-7777,
            thread_id=333,
            from_user_id=42,
            text="/newt",
            date=None,
            is_topic_message=True,
        )
    )

    assert handler._sent
    text = handler._sent[-1]
    assert "Can't start a fresh `/newt` yet." in text
    assert "vendor/sdk" in text
    assert "changed.txt" in text
    assert "Commit or stash local changes" in text


@pytest.mark.anyio
async def test_newt_runs_hub_setup_commands_for_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from codex_autorunner.core.hub_control_plane.models import (
        WorkspaceSetupCommandRequest,
        WorkspaceSetupCommandResult,
    )

    hub_root = tmp_path / "hub"
    workspace = hub_root / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        repo_id="base-repo",
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)

    async def _mock_run_setup_commands(
        request: WorkspaceSetupCommandRequest,
    ) -> WorkspaceSetupCommandResult:
        assert request.workspace_root == str(workspace.resolve())
        assert request.repo_id_hint == "base-repo"
        return WorkspaceSetupCommandResult(
            workspace_root=request.workspace_root,
            repo_id_hint=request.repo_id_hint,
            setup_command_count=2,
        )

    hub_client = AsyncMock()
    hub_client.run_workspace_setup_commands = _mock_run_setup_commands
    handler._hub_client = hub_client  # type: ignore[attr-defined]
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert any("Ran 2 setup command(s)." in text for text in handler._sent)


@pytest.mark.anyio
async def test_newt_infers_base_repo_from_worktree_id_when_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "base-repo--thread-chat-7777-thread-333"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch
    assert all("Failed to reset branch" not in text for text in handler._sent)


@pytest.mark.anyio
async def test_newt_infers_base_repo_from_legacy_wt_worktree_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "codex-autorunner-wt-1"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch


@pytest.mark.anyio
async def test_newt_prefers_longest_manifest_base_match_for_worktree_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "ml--infra--thread--chat-7777-thread-333"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch


@pytest.mark.anyio
async def test_newt_thread_fallback_and_workspace_state_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        workspace_id="stale-workspace-id",
        active_thread_id="stale-thread",
        thread_ids=["stale-thread"],
        thread_summaries={"stale-thread": ThreadSummary(user_preview="stale")},
        rollout_path="old-rollout",
        pending_compact_seed="old-seed",
        pending_compact_seed_thread_id="old-seed-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=909,
        message_id=808,
        chat_id=-123456,
        thread_id=None,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=False,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-123456-msg-808-upd-909-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch

    reset_snapshot = handler._router.update_snapshots[0]
    assert reset_snapshot["workspace_id"] == "stale-workspace-id"
    assert reset_snapshot["active_thread_id"] is None
    assert reset_snapshot["thread_ids"] == []
    assert reset_snapshot["thread_summaries"] == {}
    assert reset_snapshot["rollout_path"] is None
    assert reset_snapshot["pending_compact_seed"] is None
    assert reset_snapshot["pending_compact_seed_thread_id"] is None
    assert handler._sent
    assert (
        handler._sent[-1]
        .splitlines()[0]
        .startswith("Reset branch `thread-chat-123456-msg-808-upd-909-")
    )
    assert handler._sent[-1].splitlines()[1].startswith("Started new thread ")
