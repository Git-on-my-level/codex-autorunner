from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from codex_autorunner.core.ticket_linter_cli import LINTER_REL_PATH
from codex_autorunner.core.ticket_manager_cli import MANAGER_REL_PATH


def _run_linter(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / LINTER_REL_PATH), *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def _run_tool_lint(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / MANAGER_REL_PATH), "lint", *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def _assert_same_lint_result(repo: Path, *args: str) -> tuple[str, str]:
    linter_result = _run_linter(repo, *args)
    tool_result = _run_tool_lint(repo, *args)

    assert tool_result.returncode == linter_result.returncode
    assert tool_result.stdout == linter_result.stdout
    assert tool_result.stderr == linter_result.stderr

    return linter_result.stdout, linter_result.stderr


def test_lint_entrypoints_match_for_valid_queue(repo: Path) -> None:
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_parity001"\nagent: codex\ndone: false\n---\nBody\n',
        encoding="utf-8",
    )

    stdout, stderr = _assert_same_lint_result(repo)
    assert stdout == "OK: 1 ticket(s) linted.\n"
    assert stderr == ""


def test_lint_entrypoints_match_for_invalid_frontmatter(repo: Path) -> None:
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ntitle: Foo: Bar\ndone: false\n---\nBody\n",
        encoding="utf-8",
    )

    _stdout, stderr = _assert_same_lint_result(repo)
    assert "YAML parse error" in stderr


def test_lint_entrypoints_match_for_duplicate_ticket_id(repo: Path) -> None:
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    shared_ticket_id = 'ticket_id: "tkt_duplicate001"\n'
    body = (
        "---\n" f"{shared_ticket_id}" "agent: codex\n" "done: false\n" "---\n" "Body\n"
    )
    (tickets_dir / "TICKET-001.md").write_text(body, encoding="utf-8")
    (tickets_dir / "TICKET-002.md").write_text(body, encoding="utf-8")

    _stdout, stderr = _assert_same_lint_result(repo)
    assert "Duplicate ticket_id 'tkt_duplicate001'" in stderr


def test_lint_entrypoints_match_for_empty_ticket_directory(repo: Path) -> None:
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    stdout, stderr = _assert_same_lint_result(repo)
    assert stdout == ""
    assert stderr == f"No tickets found in {tickets_dir}\n"
