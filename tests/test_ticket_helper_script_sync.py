from __future__ import annotations

from pathlib import Path

import codex_autorunner.tickets.portable_lint as portable_lint_module
from codex_autorunner.core.ticket_linter_cli import (
    _SCRIPT as LINTER_SCRIPT,
)
from codex_autorunner.core.ticket_linter_cli import (
    LINT_IMPL_REL_PATH,
    LINTER_REL_PATH,
    ensure_ticket_lint_impl,
    ensure_ticket_linter,
)
from codex_autorunner.core.ticket_manager_cli import (
    _SCRIPT as MANAGER_SCRIPT,
)
from codex_autorunner.core.ticket_manager_cli import (
    MANAGER_REL_PATH,
    ensure_ticket_manager,
)


def test_shipped_linter_matches_generated_template(repo: Path) -> None:
    linter_path = ensure_ticket_linter(repo, force=True)
    assert linter_path == repo / LINTER_REL_PATH
    assert linter_path.read_text(encoding="utf-8") == LINTER_SCRIPT


def test_shipped_ticket_tool_matches_generated_template(repo: Path) -> None:
    tool_path = ensure_ticket_manager(repo, force=True)
    assert tool_path == repo / MANAGER_REL_PATH
    assert tool_path.read_text(encoding="utf-8") == MANAGER_SCRIPT


def test_shipped_ticket_lint_impl_matches_source_module(repo: Path) -> None:
    impl_path = ensure_ticket_lint_impl(repo, force=True)
    assert impl_path == repo / LINT_IMPL_REL_PATH
    source_path = Path(portable_lint_module.__file__).resolve()
    assert impl_path.read_text(encoding="utf-8") == source_path.read_text(
        encoding="utf-8"
    )
