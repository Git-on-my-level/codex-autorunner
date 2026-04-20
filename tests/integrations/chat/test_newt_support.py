from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.chat import newt_support


def test_build_discord_newt_branch_name_is_stable() -> None:
    workspace_root = Path("/tmp/workspace")
    assert (
        newt_support.build_discord_newt_branch_name("channel-1", workspace_root)
        == "thread-channel-1-be215dcee3"
    )


def test_build_telegram_newt_branch_name_uses_chat_and_thread_identity() -> None:
    workspace_root = Path("/tmp/workspace")
    assert (
        newt_support.build_telegram_newt_branch_name(
            chat_id=-7777,
            workspace_root=workspace_root,
            thread_id=333,
            message_id=200,
            update_id=100,
        )
        == "thread-chat-7777-thread-333-be215dcee3"
    )


def test_build_newt_reject_lines_mentions_submodules_and_resolution_hint() -> None:
    lines = newt_support.build_newt_reject_lines(
        command_label="/newt",
        reasons=["1 submodule change, including `vendor/sdk`"],
        submodule_paths=("vendor/sdk",),
        resolution_hint="Commit or stash local changes, then retry `/newt`.",
    )

    assert lines == [
        "Can't start a fresh `/newt` yet.",
        "",
        "Why:",
        "- 1 submodule change, including `vendor/sdk`",
        "",
        "Submodule in this workspace: `vendor/sdk`. `/newt` resets them too.",
        "",
        "Commit or stash local changes, then retry `/newt`.",
    ]


@pytest.mark.anyio
async def test_run_newt_branch_reset_runs_setup_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, str]] = []

    def _fake_reset_branch(repo_root: Path, branch_name: str) -> str:
        calls.append((repo_root, branch_name))
        return "main"

    class _FakeHubClient:
        async def run_workspace_setup_commands(self, request):
            assert request.workspace_root == "/tmp/repo"
            assert request.repo_id_hint == "repo-1"
            return type("Result", (), {"setup_command_count": 2})()

    monkeypatch.setattr(
        newt_support, "reset_branch_from_origin_main", _fake_reset_branch
    )
    monkeypatch.setattr(
        newt_support, "git_submodule_paths", lambda _repo_root: ["vendor/sdk"]
    )

    result = await newt_support.run_newt_branch_reset(
        Path("/tmp/repo"),
        "thread-123",
        repo_id_hint="repo-1",
        hub_client=_FakeHubClient(),
    )

    assert calls == [(Path("/tmp/repo"), "thread-123")]
    assert result == newt_support.NewtResetResult(
        default_branch="main",
        setup_command_count=2,
        submodule_paths=("vendor/sdk",),
    )


@pytest.mark.anyio
async def test_run_newt_branch_reset_wraps_setup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        newt_support, "reset_branch_from_origin_main", lambda *_args, **_kwargs: "main"
    )
    monkeypatch.setattr(
        newt_support, "git_submodule_paths", lambda _repo_root: ["vendor/sdk"]
    )

    class _FakeHubClient:
        async def run_workspace_setup_commands(self, _request):
            raise RuntimeError("setup exploded")

    with pytest.raises(newt_support.NewtSetupCommandsError) as exc_info:
        await newt_support.run_newt_branch_reset(
            Path("/tmp/repo"),
            "thread-123",
            hub_client=_FakeHubClient(),
        )

    assert str(exc_info.value) == "setup exploded"
    assert exc_info.value.default_branch == "main"
    assert exc_info.value.submodule_paths == ("vendor/sdk",)
