from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from codex_autorunner.core.ticket_manager_cli import MANAGER_REL_PATH


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    tool = repo / MANAGER_REL_PATH
    return subprocess.run(
        [sys.executable, str(tool), *args], cwd=repo, text=True, capture_output=True
    )


def test_tool_seeded_with_repo(repo: Path) -> None:
    tool = repo / MANAGER_REL_PATH
    assert tool.exists()
    assert tool.stat().st_mode & 0o111


def test_list_and_create_and_move(repo: Path) -> None:
    tickets = repo / ".codex-autorunner" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)

    res = _run(repo, "create", "--title", "First", "--agent", "codex")
    assert res.returncode == 0

    res = _run(repo, "create", "--title", "Second", "--agent", "codex")
    assert res.returncode == 0

    res = _run(repo, "list")
    assert "First" in res.stdout and "Second" in res.stdout

    res = _run(repo, "insert", "--before", "1")
    assert res.returncode == 0

    res = _run(repo, "move", "--start", "2", "--to", "1")
    assert res.returncode == 0

    res = _run(repo, "lint")
    assert res.returncode == 0


def test_insert_requires_anchor(repo: Path) -> None:
    tickets = repo / ".codex-autorunner" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    res = _run(repo, "insert")
    assert res.returncode != 0
