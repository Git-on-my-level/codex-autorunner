#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "codex_autorunner"

HINT_CONSTANTS = {
    "PROMPT_CONTEXT_HINT",
    "WHISPER_TRANSCRIPT_DISCLAIMER",
    "FILES_HINT_TEMPLATE",
}

CAPSULE_OR_TRANSPORT_RENDERERS = {
    "build_attachment_manifest_capsule",
    "build_artifact_delivery_capsule",
    "build_model_only_text_capsule",
    "build_prompt_writing_capsule",
    "build_whisper_disclaimer_capsule",
    "render_legacy_injected_context_transport",
}


def _collect_parents(node: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}

    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _is_assign_target(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if isinstance(parent, ast.Assign):
        return any(node is target for target in parent.targets)
    if isinstance(parent, ast.AnnAssign):
        return node is parent.target
    return False


def _has_capsule_or_transport_ancestor(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Name) and func.id in CAPSULE_OR_TRANSPORT_RENDERERS:
                return True
            if (
                isinstance(func, ast.Attribute)
                and func.attr in CAPSULE_OR_TRANSPORT_RENDERERS
            ):
                return True
    return False


def _is_in_comparison(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if not isinstance(parent, ast.Compare):
        return False
    return any(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)


def _check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"{path}: failed to parse ({exc})"]
    parents = _collect_parents(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        if node.id not in HINT_CONSTANTS:
            continue
        if _is_assign_target(node, parents):
            continue
        if _has_capsule_or_transport_ancestor(node, parents):
            continue
        if _is_in_comparison(node, parents):
            continue
        errors.append(
            f"{path}:{node.lineno}:{node.col_offset + 1} "
            f"{node.id} must be passed through a capsule or transport renderer."
        )
    return errors


def main() -> int:
    if not SRC_ROOT.exists():
        print(f"Missing src root: {SRC_ROOT}")
        return 1
    failures: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        failures.extend(_check_file(path))
    if failures:
        print("Injected context hint check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Injected context hint check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
