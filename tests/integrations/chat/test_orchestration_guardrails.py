from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]


def _parse_function(path: Path, function_name: str) -> ast.AsyncFunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found in {path}")


def _find_nested_async_function(
    function_node: ast.AsyncFunctionDef, nested_name: str
) -> ast.AsyncFunctionDef:
    for node in function_node.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == nested_name:
            return node
    raise AssertionError(f"{nested_name} not found inside {function_node.name}")


def _call_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _collect_call_names(nodes: list[ast.stmt]) -> list[str]:
    names: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # type: ignore[override]
            name = _call_name(node.func)
            if isinstance(name, str):
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


def test_discord_ordinary_turn_entrypoint_routes_only_via_shared_ingress() -> None:
    path = REPO_ROOT / "src/codex_autorunner/integrations/discord/message_turns.py"
    function_node = _parse_function(path, "handle_message_event")
    nested_submit = _find_nested_async_function(function_node, "_submit_thread_message")

    top_level_calls = _collect_call_names(function_node.body)
    nested_calls = _collect_call_names(nested_submit.body)

    assert "build_surface_orchestration_ingress" in top_level_calls
    assert "ingress.submit_message" in top_level_calls
    assert "service._run_agent_turn_for_message" not in top_level_calls
    assert "service._run_agent_turn_for_message" in nested_calls


def test_telegram_ordinary_turn_entrypoint_routes_only_via_shared_ingress() -> None:
    path = REPO_ROOT / "src/codex_autorunner/integrations/telegram/handlers/messages.py"
    function_node = _parse_function(path, "handle_message_inner")
    nested_submit = _find_nested_async_function(function_node, "_submit_thread_message")
    nested_work = _find_nested_async_function(function_node, "work")

    top_level_calls = _collect_call_names(function_node.body)
    nested_calls = _collect_call_names(nested_submit.body)
    work_calls = _collect_call_names(nested_work.body)

    assert "build_surface_orchestration_ingress" in top_level_calls
    assert "_submit_thread_message_core" not in top_level_calls
    assert "_submit_thread_message_core" in nested_calls
    assert "ingress.submit_message" in work_calls
