from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FunctionNode = ast.AST


def _parse_function(path: Path, function_name: str) -> FunctionNode:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == function_name
        ):
            return node
    raise AssertionError(f"{function_name} not found in {path}")


def _find_nested_async_function(
    function_node: FunctionNode, nested_name: str
) -> FunctionNode:
    for node in function_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == nested_name
        ):
            return node
    raise AssertionError(f"{nested_name} not found inside {function_node.name}")


def _resolve_alias(name: str, aliases: dict[str, str]) -> str:
    resolved = name
    seen: set[str] = set()
    while resolved in aliases and resolved not in seen:
        seen.add(resolved)
        resolved = aliases[resolved]
    return resolved


def _expr_name(node: ast.AST, aliases: dict[str, str] | None = None) -> str | None:
    alias_map = aliases or {}
    if isinstance(node, ast.Name):
        return _resolve_alias(node.id, alias_map)
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value, alias_map)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _collect_aliases(nodes: list[ast.stmt]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            value_name = _expr_name(node.value, aliases)
            if value_name is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = value_name
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value_name = (
                _expr_name(node.value, aliases) if node.value is not None else None
            )
            if value_name is not None:
                aliases[node.target.id] = value_name
    return aliases


def _collect_call_names(
    nodes: list[ast.stmt],
    *,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    names: list[str] = []
    alias_map = aliases or {}

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # type: ignore[override]
            name = _expr_name(node.func, alias_map)
            if name is not None:
                names.append(name)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # type: ignore[override]
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # type: ignore[override]
            return

    visitor = _Visitor()
    for statement in nodes:
        visitor.visit(statement)
    return names


def _collect_local_functions(function_node: FunctionNode) -> dict[str, FunctionNode]:
    local_functions: dict[str, FunctionNode] = {}

    def _visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_functions.setdefault(node.name, node)
                _visit(node.body)

    _visit(function_node.body)
    return local_functions


def _collect_reachable_call_names(
    function_node: FunctionNode,
    *,
    skip_helpers: set[str] | None = None,
) -> set[str]:
    local_functions = _collect_local_functions(function_node)
    excluded = skip_helpers or set()
    reachable: set[str] = set()
    visited: set[str] = set()

    def _visit(node_id: str, node: FunctionNode) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        aliases = _collect_aliases(node.body)
        for call_name in _collect_call_names(node.body, aliases=aliases):
            reachable.add(call_name)
            if call_name in excluded:
                continue
            helper = local_functions.get(call_name)
            if helper is not None:
                _visit(call_name, helper)

    _visit("__root__", function_node)
    return reachable


def test_discord_ordinary_turn_entrypoint_routes_only_via_shared_ingress() -> None:
    path = REPO_ROOT / "src/codex_autorunner/integrations/discord/message_turns.py"
    function_node = _parse_function(path, "handle_message_event")
    submit_helper = _parse_function(path, "_submit_discord_thread_message")

    entrypoint_calls = _collect_reachable_call_names(function_node)
    helper_calls = _collect_reachable_call_names(submit_helper)

    assert "_build_discord_surface_ingress" in entrypoint_calls
    assert "ingress.submit_message" in entrypoint_calls
    assert "service._run_agent_turn_for_message" not in entrypoint_calls
    assert "dispatch.service._run_agent_turn_for_message" in helper_calls


def test_telegram_ordinary_turn_entrypoint_routes_only_via_shared_ingress() -> None:
    path = REPO_ROOT / "src/codex_autorunner/integrations/telegram/handlers/messages.py"
    function_node = _parse_function(path, "handle_message_inner")
    dispatch_helper = _parse_function(path, "_submit_telegram_surface_turn")

    entrypoint_calls = _collect_reachable_call_names(function_node)
    helper_calls = _collect_reachable_call_names(dispatch_helper)

    assert "_enqueue_or_run_topic_call" in entrypoint_calls
    assert "_submit_thread_message_core" not in entrypoint_calls
    assert "_build_telegram_surface_ingress" in helper_calls
    assert "ingress.submit_message" in helper_calls
    assert "_submit_telegram_thread_message" in helper_calls
