from __future__ import annotations

import ast
import subprocess
import sys
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
    "src/codex_autorunner/integrations/discord/service.py",
}
HANDLER_FACING_RUNTIME_MODULES = {
    "src/codex_autorunner/integrations/discord/interaction_dispatch.py",
    "src/codex_autorunner/integrations/discord/interaction_registry.py",
    "src/codex_autorunner/integrations/discord/pma_commands.py",
    "src/codex_autorunner/integrations/discord/workspace_commands.py",
    *{
        path.as_posix()
        for path in (DISCORD_DIR / "car_handlers").glob("*.py")
        if path.name != "__init__.py"
    },
}
HANDLER_FACING_PRIVATE_RUNTIME_METHODS = {
    "_respond_ephemeral",
    "_respond_public",
    "_respond_with_components",
    "_respond_with_components_public",
    "_respond_autocomplete",
    "_defer_ephemeral",
    "_defer_public",
    "_defer_component_update",
    "_send_or_respond_ephemeral",
    "_send_or_respond_public",
    "_send_or_respond_with_components_ephemeral",
    "_send_or_respond_with_components_public",
    "_send_or_update_component_message",
    "_update_component_message",
    "_send_followup_ephemeral",
    "_send_followup_public",
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


def _function_node(relative_path: str, function_name: str) -> ast.AST:
    repo_root = DISCORD_DIR.parents[3]
    path = repo_root / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                return node
    raise AssertionError(f"Function {function_name!r} not found in {relative_path}")


def _function_has_call(
    relative_path: str, function_name: str, callee_name: str
) -> bool:
    node = _function_node(relative_path, function_name)
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == callee_name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == callee_name:
            return True
    return False


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


def test_contract_handler_facing_modules_do_not_call_private_runtime_methods() -> None:
    users = _attribute_call_users(HANDLER_FACING_PRIVATE_RUNTIME_METHODS)
    forbidden = {
        module: methods
        for module, methods in users.items()
        if module in HANDLER_FACING_RUNTIME_MODULES
    }

    assert forbidden == {}


def test_contract_leaf_runtime_modules_import_without_discord_service_side_effects() -> (
    None
):
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib; "
                "importlib.import_module('codex_autorunner.integrations.discord.interaction_runtime'); "
                "importlib.import_module('codex_autorunner.integrations.discord.interaction_session'); "
                "importlib.import_module('codex_autorunner.integrations.discord.effects')"
            ),
        ],
        check=True,
        cwd=DISCORD_DIR.parents[3],
    )


def test_contract_legacy_normalized_interaction_path_is_removed() -> None:
    assert "handle_normalized_interaction" not in _module_function_names(
        "src/codex_autorunner/integrations/discord/interaction_dispatch.py"
    )
    assert "_handle_normalized_interaction" not in _module_function_names(
        "src/codex_autorunner/integrations/discord/service.py"
    )


EXTRACTED_MODULE_SEAMS: dict[str, set[str]] = {
    "src/codex_autorunner/integrations/discord/hub_handshake.py": {
        "perform_hub_handshake",
        "HubHandshakeResult",
    },
    "src/codex_autorunner/integrations/discord/service_lifecycle.py": {
        "service_uptime_ms",
        "is_within_cold_start_window",
        "reap_managed_processes",
    },
    "src/codex_autorunner/integrations/discord/outbox.py": {
        "start_outbox_delivery_loop",
    },
    "src/codex_autorunner/integrations/discord/progress_leases.py": {
        "DiscordTurnStartupFailure",
    },
    "src/codex_autorunner/integrations/discord/managed_thread_routing.py": {
        "resolve_managed_thread_turn",
    },
    "src/codex_autorunner/integrations/discord/interaction_dispatch.py": {
        "execute_ingressed_interaction",
        "handle_component_interaction",
        "handle_modal_submit_interaction",
        "handle_autocomplete_interaction",
    },
    "src/codex_autorunner/integrations/discord/interaction_slash_builders.py": {
        "ROOT_COMMANDS",
    },
    "src/codex_autorunner/integrations/discord/interaction_component_handlers.py": {
        "BIND_SELECT_CUSTOM_ID",
    },
}


def test_contract_extracted_modules_are_importable() -> None:
    import importlib

    for module_path in EXTRACTED_MODULE_SEAMS:
        module_name = (
            module_path.replace("src/", "").replace("/", ".").removesuffix(".py")
        )
        importlib.import_module(module_name)


def test_contract_hub_handshake_does_not_import_gateway_or_rest() -> None:
    tree = ast.parse(
        (DISCORD_DIR / "hub_handshake.py").read_text(encoding="utf-8"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (
                node.module is None or "gateway" not in node.module
            ), f"hub_handshake.py must not import from gateway: {node.module}"
            assert (
                node.module is None or "rest" not in node.module.split(".")[-1]
            ), f"hub_handshake.py must not import from rest: {node.module}"


def test_contract_service_lifecycle_does_not_import_interaction_session() -> None:
    tree = ast.parse(
        (DISCORD_DIR / "service_lifecycle.py").read_text(encoding="utf-8"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (
                node.module is None or "interaction_session" not in node.module
            ), f"service_lifecycle.py must not import interaction_session: {node.module}"
            assert (
                node.module is None or "response_helpers" not in node.module
            ), f"service_lifecycle.py must not import response_helpers: {node.module}"


def test_contract_outbox_does_not_import_command_runner() -> None:
    tree = ast.parse(
        (DISCORD_DIR / "outbox.py").read_text(encoding="utf-8"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (
                node.module is None or "command_runner" not in node.module
            ), f"outbox.py must not import command_runner: {node.module}"
            assert (
                node.module is None or "ingress" not in node.module.split(".")[-1]
            ), f"outbox.py must not import ingress: {node.module}"


def test_contract_interaction_dispatch_delegates_to_registry() -> None:
    source = (DISCORD_DIR / "interaction_dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    has_registry_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if "interaction_registry" in node.module:
                has_registry_import = True
    assert (
        has_registry_import
    ), "interaction_dispatch.py must import from interaction_registry for routing"


def test_contract_extracted_handlers_use_interaction_runtime_not_raw_rest() -> None:
    handler_modules = [
        DISCORD_DIR / "interaction_slash_builders.py",
        DISCORD_DIR / "interaction_component_handlers.py",
    ]
    for path in handler_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in (
                "create_interaction_response",
                "create_followup_message",
                "edit_original_interaction_response",
            ):
                raise AssertionError(
                    f"{path.name} must not call raw REST primitive {func.attr}"
                )


def test_contract_ingress_does_not_import_effects_or_session() -> None:
    tree = ast.parse(
        (DISCORD_DIR / "ingress.py").read_text(encoding="utf-8"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (
                node.module is None or "effects" not in node.module.split(".")[-1]
            ), f"ingress.py must not import effects: {node.module}"
            assert (
                node.module is None
                or "interaction_session" not in node.module.split(".")[-1]
            ), f"ingress.py must not import interaction_session: {node.module}"
            assert (
                node.module is None
                or "response_helpers" not in node.module.split(".")[-1]
            ), f"ingress.py must not import response_helpers: {node.module}"


def test_contract_command_runner_does_not_import_effects_or_session() -> None:
    tree = ast.parse(
        (DISCORD_DIR / "command_runner.py").read_text(encoding="utf-8"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (
                node.module is None or "effects" not in node.module.split(".")[-1]
            ), f"command_runner.py must not import effects: {node.module}"
            assert (
                node.module is None
                or "interaction_session" not in node.module.split(".")[-1]
            ), f"command_runner.py must not import interaction_session: {node.module}"


def test_contract_state_store_does_not_import_orchestration_service() -> None:
    tree = ast.parse(
        (DISCORD_DIR / "state.py").read_text(encoding="utf-8"),
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "orchestration.service" in node.module:
                raise AssertionError(
                    f"state.py must not import orchestration.service: {node.module}"
                )


def test_contract_long_running_component_handlers_show_immediate_progress() -> None:
    required_helper = "defer_and_update_runtime_component_message"
    targets = {
        "src/codex_autorunner/integrations/discord/car_handlers/session_commands.py": (
            "handle_car_newt_hard_reset",
        ),
        "src/codex_autorunner/integrations/discord/flow_commands.py": (
            "handle_flow_button",
        ),
        "src/codex_autorunner/integrations/discord/interaction_component_handlers.py": (
            "_handle_flow_action_select_component",
        ),
        "src/codex_autorunner/integrations/discord/car_handlers/queue_interrupt_handlers.py": (
            "handle_cancel_turn_button",
            "handle_cancel_queued_turn_button",
            "handle_queued_turn_interrupt_send_button",
            "handle_queue_cancel_button",
            "handle_queue_interrupt_send_button",
        ),
    }

    missing = {
        relative_path: [
            function_name
            for function_name in function_names
            if not _function_has_call(relative_path, function_name, required_helper)
        ]
        for relative_path, function_names in targets.items()
    }
    missing = {path: names for path, names in missing.items() if names}

    assert missing == {}
