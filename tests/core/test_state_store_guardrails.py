from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "codex_autorunner"
RAW_STATE_PATH_PATTERN = re.compile(
    r'"\.codex-autorunner"\s*/\s*"(?:flows\.db|state\.sqlite3|manifest\.yml)"'
    r'|"\.codex-autorunner/(?:discord_state|telegram_state)\.sqlite3"'
)
RAW_ALTER_ADD_COLUMN_PATTERN = re.compile(r"ALTER TABLE .*ADD COLUMN")

ALLOWED_RAW_STATE_PATH_FILES = {
    "src/codex_autorunner/core/config_layering.py",
    "src/codex_autorunner/core/flows/pause_dispatch.py",
    "src/codex_autorunner/core/hub_repo_projection.py",
    "src/codex_autorunner/core/pr_binding_runtime.py",
    "src/codex_autorunner/flows/ticket_flow/definition.py",
    "src/codex_autorunner/integrations/agents/agent_pool_impl.py",
    "src/codex_autorunner/integrations/discord/config.py",
    "src/codex_autorunner/integrations/discord/flow_watchers.py",
    "src/codex_autorunner/integrations/discord/outbox.py",
    "src/codex_autorunner/integrations/discord/service.py",
    "src/codex_autorunner/integrations/github/polling_discovery.py",
    "src/codex_autorunner/integrations/telegram/config.py",
    "src/codex_autorunner/integrations/telegram/doctor.py",
    "src/codex_autorunner/integrations/telegram/handlers/commands/workspace_session_commands.py",
    "src/codex_autorunner/integrations/telegram/ticket_flow_bridge.py",
    "src/codex_autorunner/surfaces/web/routes/hub_repo_routes/channels.py",
}
ALLOWED_RAW_ALTER_ADD_COLUMN_FILES = {
    "src/codex_autorunner/core/sqlite_utils.py",
}


def _matching_files(pattern: re.Pattern[str]) -> set[str]:
    matches: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern.search(text):
            matches.add(path.relative_to(REPO_ROOT).as_posix())
    return matches


def test_raw_state_path_literals_are_allowlisted() -> None:
    assert _matching_files(RAW_STATE_PATH_PATTERN) == ALLOWED_RAW_STATE_PATH_FILES


def test_inline_alter_table_add_column_calls_are_centralized() -> None:
    assert (
        _matching_files(RAW_ALTER_ADD_COLUMN_PATTERN)
        == ALLOWED_RAW_ALTER_ADD_COLUMN_FILES
    )
