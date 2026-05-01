from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.support.git_test_helpers import init_git_repo as _init_git_repo


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_no_codex_autorunner_staged.py"
    )


def _run_guard(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_script_path())],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )


def test_guard_rejects_staged_codex_autorunner_files(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    car_dir = tmp_path / ".codex-autorunner"
    car_dir.mkdir()
    (car_dir / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    (car_dir / "tickets").mkdir()
    blocked = car_dir / "tickets" / "TICKET-001.md"
    blocked.write_text("ticket\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", "-f", ".codex-autorunner/tickets/TICKET-001.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert ".codex-autorunner/tickets/TICKET-001.md" in result.stderr


def test_guard_allows_only_gitignore_under_codex_autorunner(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    car_dir = tmp_path / ".codex-autorunner"
    car_dir.mkdir()
    (car_dir / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", ".codex-autorunner/.gitignore"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_allows_staged_deletions_under_codex_autorunner(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    car_dir = tmp_path / ".codex-autorunner"
    (car_dir / "tickets").mkdir(parents=True)
    tracked = car_dir / "tickets" / "TICKET-001.md"
    tracked.write_text("ticket\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", ".codex-autorunner/tickets/TICKET-001.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed tracked car file"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    tracked.unlink()
    subprocess.run(
        ["git", "add", "-u", ".codex-autorunner/tickets/TICKET-001.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
