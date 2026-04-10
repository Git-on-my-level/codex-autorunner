from __future__ import annotations

import ast
from pathlib import Path

DISCORD_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "codex_autorunner"
    / "integrations"
    / "discord"
)
RAW_RESPONSE_PRIMITIVES = {
    "create_interaction_response",
    "create_followup_message",
    "edit_original_interaction_response",
}
ALLOWED_RAW_RESPONSE_MODULES = {
    "src/codex_autorunner/integrations/discord/adapter.py",
    "src/codex_autorunner/integrations/discord/interaction_session.py",
}


def _raw_response_users() -> dict[str, set[str]]:
    repo_root = DISCORD_DIR.parents[3]
    users: dict[str, set[str]] = {}

    for path in sorted(DISCORD_DIR.glob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in RAW_RESPONSE_PRIMITIVES:
                continue
            users.setdefault(relative, set()).add(func.attr)

    return users


def test_contract_only_boundary_modules_touch_raw_discord_response_primitives() -> None:
    users = _raw_response_users()

    assert users, "expected at least one raw Discord response primitive user"
    assert set(users) == ALLOWED_RAW_RESPONSE_MODULES


def test_contract_handler_modules_do_not_own_ack_or_followup_primitives() -> None:
    users = _raw_response_users()
    forbidden = {
        module: methods
        for module, methods in users.items()
        if module not in ALLOWED_RAW_RESPONSE_MODULES
    }

    assert forbidden == {}
