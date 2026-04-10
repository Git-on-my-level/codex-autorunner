from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from codex_autorunner.core import git_utils


def _ok_proc(stdout: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


def test_reset_branch_from_origin_main_uses_origin_default_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path, int, bool]] = []

    def _fake_git_default_branch(_repo_root: Path) -> str:
        return "master"

    def _fake_run_git(
        args: list[str],
        cwd: Path,
        *,
        timeout_seconds: int = 30,
        check: bool = False,
    ) -> CompletedProcess[str]:
        calls.append((args, cwd, timeout_seconds, check))
        if args == ["status", "--porcelain"]:
            return _ok_proc(stdout="")
        if args == ["fetch", "--prune", "origin"]:
            return _ok_proc()
        if args == ["checkout", "-B", "thread-123", "origin/master"]:
            return _ok_proc()
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(git_utils, "git_default_branch", _fake_git_default_branch)
    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)

    repo_root = Path("/tmp/repo")
    git_utils.reset_branch_from_origin_main(repo_root, "thread-123")

    assert calls == [
        (["status", "--porcelain"], repo_root, 30, True),
        (["fetch", "--prune", "origin"], repo_root, 120, True),
        (["checkout", "-B", "thread-123", "origin/master"], repo_root, 60, True),
    ]


def test_reset_branch_from_origin_main_raises_when_origin_default_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run_git(
        args: list[str],
        _cwd: Path,
        *,
        timeout_seconds: int = 30,
        check: bool = False,
    ) -> CompletedProcess[str]:
        _ = timeout_seconds, check
        if args == ["status", "--porcelain"]:
            return _ok_proc(stdout="")
        if args == ["fetch", "--prune", "origin"]:
            return _ok_proc()
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    monkeypatch.setattr(git_utils, "git_default_branch", lambda _repo_root: None)

    with pytest.raises(
        git_utils.GitError, match="unable to resolve origin default branch"
    ):
        git_utils.reset_branch_from_origin_main(Path("/tmp/repo"), "thread-123")


def test_reset_branch_from_origin_main_raises_when_worktree_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run_git(
        args: list[str],
        _cwd: Path,
        *,
        timeout_seconds: int = 30,
        check: bool = False,
    ) -> CompletedProcess[str]:
        _ = timeout_seconds, check
        if args == ["status", "--porcelain"]:
            return _ok_proc(stdout=" M changed.txt\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)

    with pytest.raises(
        git_utils.GitError,
        match="working tree has uncommitted changes; commit or stash before /newt",
    ):
        git_utils.reset_branch_from_origin_main(Path("/tmp/repo"), "thread-123")


def test_describe_newt_reject_reasons_summarizes_git_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        git_utils,
        "git_status_porcelain",
        lambda _repo_root: "M  staged.py\n M unstaged.py\n?? .tmp/\nUU conflict.txt\n",
    )

    assert git_utils.describe_newt_reject_reasons(Path("/tmp/repo")) == [
        "1 merge conflict, including `conflict.txt`",
        "1 staged tracked change, including `staged.py`",
        "1 unstaged tracked change, including `unstaged.py`",
        "1 untracked path, including `.tmp/`",
    ]
