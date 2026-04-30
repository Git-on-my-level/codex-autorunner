"""Shared git test helpers for initializing test repositories."""

from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.git_utils import run_git


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], path, check=True)
    run_git(["config", "user.email", "test@example.com"], path, check=True)
    run_git(["config", "user.name", "Test User"], path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    run_git(["add", "README.md"], path, check=True)
    run_git(["commit", "-m", "init"], path, check=True)
