from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.ticket_manager_cli import (
    _SCRIPT as MANAGER_SCRIPT,
)
from codex_autorunner.core.ticket_manager_cli import (
    MANAGER_REL_PATH,
    ensure_ticket_manager,
)


def test_shipped_ticket_tool_matches_generated_template(repo: Path) -> None:
    tool_path = ensure_ticket_manager(repo, force=True)
    assert tool_path == repo / MANAGER_REL_PATH
    assert tool_path.read_text(encoding="utf-8") == MANAGER_SCRIPT


def test_seeded_ticket_bin_uses_cli_for_lint(repo: Path) -> None:
    bin_dir = repo / ".codex-autorunner" / "bin"
    assert {path.name for path in bin_dir.iterdir() if path.is_file()} == {
        "car",
        "ticket_tool.py",
    }
    assert 'sub.add_parser("lint"' not in MANAGER_SCRIPT
    assert "cmd_lint" not in MANAGER_SCRIPT
