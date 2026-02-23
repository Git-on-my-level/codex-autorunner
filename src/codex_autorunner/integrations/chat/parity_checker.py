"""Static source-inspection parity checks for chat command handling."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .command_contract import COMMAND_CONTRACT, CommandContractEntry

_DISCORD_SERVICE_PATH = Path("src/codex_autorunner/integrations/discord/service.py")
_TELEGRAM_TRIGGER_MODE_PATH = Path(
    "src/codex_autorunner/integrations/telegram/trigger_mode.py"
)
_TELEGRAM_MESSAGES_PATH = Path(
    "src/codex_autorunner/integrations/telegram/handlers/messages.py"
)
_FUNCTION_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class ParityCheckResult:
    id: str
    passed: bool
    message: str
    metadata: dict[str, Any]


def run_parity_checks(
    *,
    repo_root: Path | None = None,
    contract: Sequence[CommandContractEntry] = COMMAND_CONTRACT,
) -> tuple[ParityCheckResult, ...]:
    source_paths = {
        "discord_service": _resolve_source_path(
            repo_root=repo_root,
            repo_relative_path=_DISCORD_SERVICE_PATH,
        ),
        "telegram_trigger_mode": _resolve_source_path(
            repo_root=repo_root,
            repo_relative_path=_TELEGRAM_TRIGGER_MODE_PATH,
        ),
        "telegram_messages": _resolve_source_path(
            repo_root=repo_root,
            repo_relative_path=_TELEGRAM_MESSAGES_PATH,
        ),
    }
    if repo_root is None and any(path is None for path in source_paths.values()):
        missing = [name for name, path in source_paths.items() if path is None]
        return _source_unavailable_results(missing=missing)

    discord_service_text = _read_text(source_paths["discord_service"])
    telegram_trigger_mode_text = _read_text(source_paths["telegram_trigger_mode"])
    telegram_messages_text = _read_text(source_paths["telegram_messages"])

    discord_service_ast = _parse_module(discord_service_text)
    telegram_trigger_mode_ast = _parse_module(telegram_trigger_mode_text)
    telegram_messages_ast = _parse_module(telegram_messages_text)

    return (
        _check_discord_contract_commands_routed(
            contract=contract,
            discord_service_ast=discord_service_ast,
        ),
        _check_discord_known_commands_not_in_generic_fallback(
            discord_service_ast=discord_service_ast,
        ),
        _check_discord_canonicalize_command_ingress_usage(
            discord_service_ast=discord_service_ast,
        ),
        _check_discord_interaction_component_guard_paths(
            discord_service_ast=discord_service_ast,
        ),
        _check_shared_plain_text_turn_policy_usage(
            discord_service_ast=discord_service_ast,
            telegram_trigger_mode_ast=telegram_trigger_mode_ast,
            telegram_messages_ast=telegram_messages_ast,
        ),
    )


def _resolve_source_path(
    *,
    repo_root: Path | None,
    repo_relative_path: Path,
) -> Path | None:
    package_relative_path = _package_relative_path(repo_relative_path)
    candidates: list[Path] = []

    if repo_root is not None:
        candidates.extend(
            (
                repo_root / repo_relative_path,
                repo_root / package_relative_path,
            )
        )
    else:
        module_path = Path(__file__).resolve()
        package_root = module_path.parents[2]
        candidates.append(package_root.parent / package_relative_path)
        for parent in module_path.parents:
            if not (parent / ".git").exists():
                continue
            candidates.extend(
                (
                    parent / repo_relative_path,
                    parent / package_relative_path,
                )
            )
            break

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _package_relative_path(path: Path) -> Path:
    if path.parts and path.parts[0] == "src":
        return Path(*path.parts[1:])
    return path


def _source_unavailable_results(
    *, missing: Sequence[str]
) -> tuple[ParityCheckResult, ...]:
    message = (
        "Skipped parity check: chat source files are unavailable outside a checkout."
    )
    metadata = {
        "skipped": True,
        "reason": "source_files_unavailable",
        "missing_sources": list(missing),
    }
    return (
        ParityCheckResult(
            id="discord.contract_commands_routed",
            passed=True,
            message=message,
            metadata=metadata,
        ),
        ParityCheckResult(
            id="discord.no_generic_fallback_leak",
            passed=True,
            message=message,
            metadata=metadata,
        ),
        ParityCheckResult(
            id="discord.canonical_command_ingress_usage",
            passed=True,
            message=message,
            metadata=metadata,
        ),
        ParityCheckResult(
            id="discord.interaction_component_guard_paths",
            passed=True,
            message=message,
            metadata=metadata,
        ),
        ParityCheckResult(
            id="chat.shared_plain_text_turn_policy_usage",
            passed=True,
            message=message,
            metadata=metadata,
        ),
    )


def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_module(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _check_discord_contract_commands_routed(
    *,
    contract: Sequence[CommandContractEntry],
    discord_service_ast: ast.Module | None,
) -> ParityCheckResult:
    missing_ids: list[str] = []

    for entry in contract:
        if _is_command_routed_in_discord_service(entry, discord_service_ast):
            continue
        missing_ids.append(entry.id)

    passed = not missing_ids
    if passed:
        message = "All contract commands are routed in Discord command handling."
    else:
        message = "Missing Discord route handling for one or more contract commands."

    return ParityCheckResult(
        id="discord.contract_commands_routed",
        passed=passed,
        message=message,
        metadata={
            "expected_ids": [entry.id for entry in contract],
            "missing_ids": missing_ids,
        },
    )


def _is_command_routed_in_discord_service(
    entry: CommandContractEntry,
    discord_service_ast: ast.Module | None,
) -> bool:
    if discord_service_ast is None:
        return False

    prefix = entry.path[0] if entry.path else ""

    if prefix == "car":
        return _module_has_command_path_route(discord_service_ast, entry.path)

    if prefix == "pma":
        subcommand = entry.path[1] if len(entry.path) > 1 else ""
        return _module_has_pma_subcommand_route(discord_service_ast, subcommand)

    return False


def _check_discord_known_commands_not_in_generic_fallback(
    *,
    discord_service_ast: ast.Module | None,
) -> ParityCheckResult:
    normalized_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_normalized_interaction",
    )
    interaction_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_interaction",
    )

    checks = {
        "normalized_car_prefix_guard": _has_prefix_guard_in_functions(
            normalized_handlers,
            prefix="car",
            require_ingress_not_none=True,
        ),
        "normalized_pma_prefix_guard": _has_prefix_guard_in_functions(
            normalized_handlers,
            prefix="pma",
            require_ingress_not_none=True,
        ),
        "interaction_car_prefix_guard": _has_prefix_guard_in_functions(
            interaction_handlers,
            prefix="car",
            require_ingress_not_none=False,
        ),
        "interaction_pma_prefix_guard": _has_prefix_guard_in_functions(
            interaction_handlers,
            prefix="pma",
            require_ingress_not_none=False,
        ),
        "generic_fallback_present": _module_has_string_literal(
            discord_service_ast,
            exact="Command not implemented yet for Discord.",
        ),
        "car_specific_fallback_present": _module_has_string_literal(
            discord_service_ast,
            contains="Unknown car subcommand:",
        ),
        "pma_specific_fallback_present": _module_has_string_literal(
            discord_service_ast,
            exact="Unknown PMA subcommand. Use on, off, or status.",
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = "Known Discord command prefixes are guarded before generic fallback."
    else:
        message = "Discord fallback guard structure is incomplete for known commands."

    return ParityCheckResult(
        id="discord.no_generic_fallback_leak",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _check_discord_canonicalize_command_ingress_usage(
    *,
    discord_service_ast: ast.Module | None,
) -> ParityCheckResult:
    normalized_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_normalized_interaction",
    )
    interaction_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_interaction",
    )

    checks = {
        "import_present": _module_imports_name(
            discord_service_ast,
            module_suffix="integrations.chat.command_ingress",
            name="canonicalize_command_ingress",
        ),
        "normalized_interaction_call_present": _has_call_in_functions(
            normalized_handlers,
            callee_name="canonicalize_command_ingress",
            required_keywords={
                "command": lambda expr: _is_dict_get(
                    expr,
                    object_name="payload_data",
                    key="command",
                )
                or isinstance(expr, ast.Name),
                "options": lambda expr: _is_dict_get(
                    expr,
                    object_name="payload_data",
                    key="options",
                )
                or isinstance(expr, ast.Name),
            },
        ),
        "interaction_call_present": _has_call_in_functions(
            interaction_handlers,
            callee_name="canonicalize_command_ingress",
            required_keywords={
                "command_path": lambda expr: _is_name(expr, "command_path")
                or isinstance(expr, ast.Name),
                "options": lambda expr: _is_name(expr, "options")
                or isinstance(expr, ast.Name),
            },
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = "Discord ingress paths use shared canonical command normalization."
    else:
        message = "Discord canonical command normalization is missing in expected ingress paths."

    return ParityCheckResult(
        id="discord.canonical_command_ingress_usage",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _check_shared_plain_text_turn_policy_usage(
    *,
    discord_service_ast: ast.Module | None,
    telegram_trigger_mode_ast: ast.Module | None,
    telegram_messages_ast: ast.Module | None,
) -> ParityCheckResult:
    discord_message_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_message_event",
    )
    telegram_trigger_handlers = _find_functions_by_name(
        telegram_trigger_mode_ast,
        "should_trigger_run",
    )

    checks = {
        "discord_shared_policy_call": _has_plain_text_turn_policy_call(
            discord_message_handlers,
            mode="always",
        ),
        "telegram_shared_policy_call": _has_plain_text_turn_policy_call(
            telegram_trigger_handlers,
            mode="mentions",
        ),
        "telegram_trigger_bridge": (
            _module_imports_name(
                telegram_messages_ast,
                module_suffix="trigger_mode",
                name="should_trigger_run",
            )
            and _module_has_call(
                telegram_messages_ast,
                callee_name="should_trigger_run",
            )
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = (
            "Telegram and Discord trigger paths use the shared plain-text turn policy."
        )
    else:
        message = "Shared plain-text turn policy usage is missing in Telegram/Discord trigger paths."

    return ParityCheckResult(
        id="chat.shared_plain_text_turn_policy_usage",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _check_discord_interaction_component_guard_paths(
    *,
    discord_service_ast: ast.Module | None,
) -> ParityCheckResult:
    interaction_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_interaction",
    )
    component_handlers = _find_functions_by_name(
        discord_service_ast,
        "_handle_component_interaction",
    )

    checks = {
        "interaction_parse_failure_response": _has_guard_response(
            interaction_handlers,
            guard_predicate=_condition_has_ingress_is_none_guard,
            response_contains="could not parse this interaction",
        ),
        "interaction_unknown_command_fallback": _has_call_with_string_argument(
            interaction_handlers,
            callee_name="_respond_ephemeral",
            exact="Command not implemented yet for Discord.",
        ),
        "interaction_unhandled_error_logged": _has_log_event_call(
            interaction_handlers,
            event_name="discord.interaction.unhandled_error",
        ),
        "interaction_unhandled_error_response": _has_call_with_string_argument(
            interaction_handlers,
            callee_name="_respond_ephemeral",
            exact="An unexpected error occurred. Please try again later.",
        ),
        "component_missing_custom_id_response": _has_guard_response(
            component_handlers,
            guard_predicate=lambda test: _condition_has_name_negation(
                test, name="custom_id"
            ),
            response_contains="could not identify this interaction action",
        ),
        "component_bind_selection_requires_value": _has_nested_guard_response(
            component_handlers,
            outer_guard_name="custom_id",
            outer_guard_value="bind_select",
            nested_guard_name="values",
            response_contains="select a repository",
        ),
        "component_flow_runs_selection_requires_value": _has_nested_guard_response(
            component_handlers,
            outer_guard_name="custom_id",
            outer_guard_value="flow_runs_select",
            nested_guard_name="values",
            response_contains="select a run",
        ),
        "component_unknown_fallback": _has_call_with_string_argument(
            component_handlers,
            callee_name="_respond_ephemeral",
            contains="Unknown component:",
        ),
        "component_unhandled_error_logged": _has_log_event_call(
            component_handlers,
            event_name="discord.component.unhandled_error",
        ),
        "component_unhandled_error_response": _has_call_with_string_argument(
            component_handlers,
            callee_name="_respond_ephemeral",
            exact="An unexpected error occurred. Please try again later.",
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = (
            "Discord interaction/component handlers keep observable guards and "
            "explicit fallback responses."
        )
    else:
        message = (
            "Discord interaction/component guard coverage is incomplete and may "
            "allow silent handling regressions."
        )

    return ParityCheckResult(
        id="discord.interaction_component_guard_paths",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _find_functions_by_name(
    tree: ast.Module | None,
    name: str,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
    if tree is None:
        return ()
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, _FUNCTION_NODE_TYPES) and node.name == name
    )


def _module_has_command_path_route(tree: ast.Module, path: tuple[str, ...]) -> bool:
    for compare in _iter_compares(tree):
        if _compare_matches_command_path_eq_tuple(compare, path):
            return True
    return False


def _module_has_pma_subcommand_route(tree: ast.Module, subcommand: str) -> bool:
    expected_functions = (
        "_handle_pma_command",
        "_handle_pma_command_from_normalized",
    )

    for function_name in expected_functions:
        functions = _find_functions_by_name(tree, function_name)
        if not functions:
            return False
        if not any(
            subcommand in _extract_subcommand_compares(function)
            for function in functions
        ):
            return False

    return True


def _extract_subcommand_compares(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    values: set[str] = set()
    for compare in _iter_compares(function):
        literal = _extract_name_eq_string(compare, "subcommand")
        if literal is not None:
            values.add(literal)
    return values


def _has_prefix_guard_in_functions(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    prefix: str,
    require_ingress_not_none: bool,
) -> bool:
    for function in functions:
        has_none_guard = (
            _function_has_ingress_none_return_guard(function)
            if require_ingress_not_none
            else False
        )
        for node in ast.walk(function):
            if not isinstance(node, ast.If):
                continue
            has_prefix = _condition_has_ingress_prefix_guard(node.test, prefix=prefix)
            if not has_prefix:
                continue
            if require_ingress_not_none and not _condition_has_ingress_not_none_guard(
                node.test
            ):
                if not has_none_guard:
                    continue
            return True
    return False


def _condition_has_ingress_prefix_guard(test: ast.expr, *, prefix: str) -> bool:
    for term in _iter_boolean_terms(test):
        if _compare_matches_ingress_prefix(term, prefix):
            return True
    return False


def _condition_has_ingress_not_none_guard(test: ast.expr) -> bool:
    for term in _iter_boolean_terms(test):
        if _is_name(term, "ingress"):
            return True
        if not isinstance(term, ast.Compare):
            continue
        if len(term.ops) != 1 or not isinstance(term.ops[0], ast.IsNot):
            continue
        left = term.left
        right = term.comparators[0]
        if (_is_name(left, "ingress") and _is_none(right)) or (
            _is_name(right, "ingress") and _is_none(left)
        ):
            return True
    return False


def _condition_has_ingress_is_none_guard(test: ast.expr) -> bool:
    for term in _iter_boolean_terms(test):
        if not isinstance(term, ast.Compare):
            continue
        if len(term.ops) != 1 or not isinstance(term.ops[0], ast.Is):
            continue
        left = term.left
        right = term.comparators[0]
        if (_is_name(left, "ingress") and _is_none(right)) or (
            _is_name(right, "ingress") and _is_none(left)
        ):
            return True
    return False


def _function_has_ingress_none_return_guard(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        if not _condition_has_ingress_is_none_guard(node.test):
            continue
        if any(isinstance(statement, ast.Return) for statement in node.body):
            return True
    return False


def _iter_boolean_terms(expr: ast.expr) -> Iterable[ast.expr]:
    if isinstance(expr, ast.BoolOp):
        for value in expr.values:
            yield from _iter_boolean_terms(value)
        return
    yield expr


def _compare_matches_ingress_prefix(expr: ast.expr, prefix: str) -> bool:
    if not isinstance(expr, ast.Compare):
        return False
    if len(expr.ops) != 1 or not isinstance(expr.ops[0], ast.Eq):
        return False
    left = expr.left
    right = expr.comparators[0]
    return (
        _is_ingress_command_path_prefix_slice(left)
        and _is_singleton_string_tuple(right, prefix)
    ) or (
        _is_ingress_command_path_prefix_slice(right)
        and _is_singleton_string_tuple(left, prefix)
    )


def _is_ingress_command_path_prefix_slice(expr: ast.expr) -> bool:
    if not isinstance(expr, ast.Subscript):
        return False
    if not (
        isinstance(expr.value, ast.Attribute)
        and expr.value.attr == "command_path"
        and _is_name(expr.value.value, "ingress")
    ):
        return False
    return _is_first_item_slice(expr.slice)


def _is_first_item_slice(slice_node: ast.slice) -> bool:
    # ast.Index exists in older Python versions and wraps the real slice node.
    index_type = getattr(ast, "Index", None)
    if index_type is not None and isinstance(slice_node, index_type):
        slice_node = slice_node.value  # type: ignore[assignment]
    if not isinstance(slice_node, ast.Slice):
        return False
    lower_ok = slice_node.lower is None or _is_int_constant(slice_node.lower, 0)
    upper_ok = _is_int_constant(slice_node.upper, 1)
    step_ok = slice_node.step is None
    return lower_ok and upper_ok and step_ok


def _module_has_string_literal(
    tree: ast.Module | None,
    *,
    exact: str | None = None,
    contains: str | None = None,
) -> bool:
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if exact is not None and node.value == exact:
            return True
        if contains is not None and contains in node.value:
            return True
    return False


def _module_imports_name(
    tree: ast.Module | None,
    *,
    module_suffix: str,
    name: str,
) -> bool:
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not module.endswith(module_suffix):
            continue
        for imported in node.names:
            if imported.name == name:
                return True
    return False


def _has_call_in_functions(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    callee_name: str,
    required_keywords: dict[str, Callable[[ast.expr], bool]],
) -> bool:
    for function in functions:
        for call in _iter_calls(function):
            if _call_name(call.func) != callee_name:
                continue
            if _call_has_required_keywords(call, required_keywords):
                return True
    return False


def _has_plain_text_turn_policy_call(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    mode: str,
) -> bool:
    return _has_call_in_functions(
        functions,
        callee_name="should_trigger_plain_text_turn",
        required_keywords={
            "mode": lambda expr: _is_string_constant(expr, mode),
            "context": _is_plain_text_context_call,
        },
    )


def _is_plain_text_context_call(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Name):
        return True
    if not isinstance(expr, ast.Call):
        return False
    return _call_name(expr.func) == "PlainTextTurnContext"


def _module_has_call(tree: ast.Module | None, *, callee_name: str) -> bool:
    if tree is None:
        return False
    for call in _iter_calls(tree):
        if _call_name(call.func) == callee_name:
            return True
    return False


def _has_call_with_string_argument(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    callee_name: str,
    exact: str | None = None,
    contains: str | None = None,
) -> bool:
    for function in functions:
        for call in _iter_calls(function):
            if _call_name(call.func) != callee_name:
                continue
            if _call_has_string_argument(call, exact=exact, contains=contains):
                return True
    return False


def _call_has_string_argument(
    call: ast.Call,
    *,
    exact: str | None = None,
    contains: str | None = None,
) -> bool:
    values = list(call.args)
    values.extend(kw.value for kw in call.keywords if kw.arg is not None)

    for value in values:
        if exact is not None and _is_string_constant(value, exact):
            return True
        if contains is not None and _expr_contains_string(value, contains=contains):
            return True
    return False


def _expr_contains_string(expr: ast.expr, *, contains: str) -> bool:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return contains in expr.value
    if isinstance(expr, ast.JoinedStr):
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if contains in value.value:
                    return True
    return False


def _has_log_event_call(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    event_name: str,
) -> bool:
    return _has_call_with_string_argument(
        functions,
        callee_name="log_event",
        exact=event_name,
    )


def _has_guard_response(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    guard_predicate: Callable[[ast.expr], bool],
    response_contains: str,
) -> bool:
    for function in functions:
        for node in ast.walk(function):
            if not isinstance(node, ast.If):
                continue
            if not guard_predicate(node.test):
                continue
            if _body_has_response_call(node.body, contains=response_contains):
                return True
    return False


def _has_nested_guard_response(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    outer_guard_name: str,
    outer_guard_value: str,
    nested_guard_name: str,
    response_contains: str,
) -> bool:
    for function in functions:
        for node in ast.walk(function):
            if not isinstance(node, ast.If):
                continue
            if not _condition_has_name_eq_string(
                node.test,
                name=outer_guard_name,
                value=outer_guard_value,
            ):
                continue
            for nested in ast.walk(node):
                if not isinstance(nested, ast.If):
                    continue
                if not _condition_has_name_negation(
                    nested.test, name=nested_guard_name
                ):
                    continue
                if _body_has_response_call(nested.body, contains=response_contains):
                    return True
    return False


def _body_has_response_call(statements: Sequence[ast.stmt], *, contains: str) -> bool:
    for statement in statements:
        for call in _iter_calls(statement):
            if _call_name(call.func) != "_respond_ephemeral":
                continue
            if _call_has_string_argument(call, contains=contains):
                return True
    return False


def _condition_has_name_negation(test: ast.expr, *, name: str) -> bool:
    for term in _iter_boolean_terms(test):
        if not isinstance(term, ast.UnaryOp) or not isinstance(term.op, ast.Not):
            continue
        if _is_name(term.operand, name):
            return True
    return False


def _condition_has_name_eq_string(test: ast.expr, *, name: str, value: str) -> bool:
    for term in _iter_boolean_terms(test):
        if not isinstance(term, ast.Compare):
            continue
        if _extract_name_eq_string(term, name) == value:
            return True
    return False


def _call_has_required_keywords(
    call: ast.Call,
    required_keywords: dict[str, Callable[[ast.expr], bool]],
) -> bool:
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    for key, predicate in required_keywords.items():
        value = keywords.get(key)
        if value is None or not predicate(value):
            return False
    return True


def _call_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _is_dict_get(expr: ast.expr, *, object_name: str, key: str) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    if not (
        isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and _is_name(expr.func.value, object_name)
    ):
        return False
    if not expr.args:
        return False
    return _is_string_constant(expr.args[0], key)


def _iter_calls(node: ast.AST) -> Iterable[ast.Call]:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


def _iter_compares(node: ast.AST) -> Iterable[ast.Compare]:
    for child in ast.walk(node):
        if isinstance(child, ast.Compare):
            yield child


def _compare_matches_command_path_eq_tuple(
    compare: ast.Compare,
    path: tuple[str, ...],
) -> bool:
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return False
    right = compare.comparators[0]
    return (
        _is_name(compare.left, "command_path") and _is_string_tuple(right, path)
    ) or (_is_name(right, "command_path") and _is_string_tuple(compare.left, path))


def _extract_name_eq_string(compare: ast.Compare, name: str) -> str | None:
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return None
    right = compare.comparators[0]
    if _is_name(compare.left, name):
        return _string_constant_value(right)
    if _is_name(right, name):
        return _string_constant_value(compare.left)
    return None


def _is_singleton_string_tuple(expr: ast.expr, value: str) -> bool:
    return _string_tuple(expr) == (value,)


def _is_string_tuple(expr: ast.expr, expected: tuple[str, ...]) -> bool:
    return _string_tuple(expr) == expected


def _string_tuple(expr: ast.expr) -> tuple[str, ...] | None:
    if not isinstance(expr, ast.Tuple):
        return None
    values: list[str] = []
    for item in expr.elts:
        literal = _string_constant_value(item)
        if literal is None:
            return None
        values.append(literal)
    return tuple(values)


def _string_constant_value(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _is_name(expr: ast.expr, expected: str) -> bool:
    return isinstance(expr, ast.Name) and expr.id == expected


def _is_string_constant(expr: ast.expr, expected: str) -> bool:
    return _string_constant_value(expr) == expected


def _is_int_constant(expr: ast.expr | None, expected: int) -> bool:
    return isinstance(expr, ast.Constant) and expr.value == expected


def _is_none(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None
