from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.ticket_linter_cli import (
    _SCRIPT as LINTER_SCRIPT,
)
from codex_autorunner.core.ticket_linter_cli import (
    LINTER_REL_PATH,
)
from codex_autorunner.core.ticket_manager_cli import (
    _SCRIPT as MANAGER_SCRIPT,
)
from codex_autorunner.core.ticket_manager_cli import (
    MANAGER_REL_PATH,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shipped_linter_matches_generated_template() -> None:
    assert (REPO_ROOT / LINTER_REL_PATH).read_text(encoding="utf-8") == LINTER_SCRIPT


def test_shipped_ticket_tool_matches_generated_template() -> None:
    assert (REPO_ROOT / MANAGER_REL_PATH).read_text(encoding="utf-8") == MANAGER_SCRIPT
