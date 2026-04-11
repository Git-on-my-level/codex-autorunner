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
LOW_LEVEL_RESPONSE_HELPERS = {
    "_interaction_has_initial_response",
    "_defer_ephemeral",
    "_defer_public",
    "_defer_component_update",
    "_send_followup_ephemeral",
    "_send_followup_public",
    "prepared_interaction_policy",
}
ALLOWED_LOW_LEVEL_RESPONSE_HELPER_MODULES = {
    "src/codex_autorunner/integrations/discord/effects.py",
    "src/codex_autorunner/integrations/discord/interaction_runtime.py",
    "src/codex_autorunner/integrations/discord/service.py",
}


def _attribute_call_users(attribute_names: set[str]) -> dict[str, set[str]]:
    repo_root = DISCORD_DIR.parents[3]
    users: dict[str, set[str]] = {}

    for path in sorted(DISCORD_DIR.rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in attribute_names:
                users.setdefault(relative, set()).add(func.attr)
                continue
            if not (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
            ):
                continue
            attr_name = node.args[1]
            if (
                isinstance(attr_name, ast.Constant)
                and isinstance(attr_name.value, str)
                and attr_name.value in attribute_names
            ):
                users.setdefault(relative, set()).add(attr_name.value)

    return users


def _raw_response_users() -> dict[str, set[str]]:
    return _attribute_call_users(RAW_RESPONSE_PRIMITIVES)


def _low_level_response_helper_users() -> dict[str, set[str]]:
    return _attribute_call_users(LOW_LEVEL_RESPONSE_HELPERS)


def _module_function_names(relative_path: str) -> set[str]:
    repo_root = DISCORD_DIR.parents[3]
    path = repo_root / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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


def test_contract_only_runtime_modules_touch_low_level_response_helpers() -> None:
    users = _low_level_response_helper_users()

    assert users, "expected low-level response helper usage to remain observable"
    assert set(users) == ALLOWED_LOW_LEVEL_RESPONSE_HELPER_MODULES


def test_contract_handler_modules_do_not_bypass_interaction_runtime_boundary() -> None:
    users = _low_level_response_helper_users()
    forbidden = {
        module: methods
        for module, methods in users.items()
        if module not in ALLOWED_LOW_LEVEL_RESPONSE_HELPER_MODULES
    }

    assert forbidden == {}


def test_contract_legacy_normalized_interaction_path_is_removed() -> None:
    assert "handle_normalized_interaction" not in _module_function_names(
        "src/codex_autorunner/integrations/discord/interaction_dispatch.py"
    )
    assert "_handle_normalized_interaction" not in _module_function_names(
        "src/codex_autorunner/integrations/discord/service.py"
    )
