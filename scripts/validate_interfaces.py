#!/usr/bin/env python3
"""Validate that TypeScript hub interfaces match Python hub data contracts."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.drift_check_utils import (  # noqa: E402
    emit_cli_report,
    load_json_document,
    load_text_document,
)

SCHEMA_BINDINGS = {
    "RepoSnapshot": {
        "typescript": "HubRepo",
        "python": "RepoSnapshot",
        "python_extra": {
            "mounted",
            "mount_error",
            "ticket_flow",
            "ticket_flow_display",
        },
    },
    "AgentWorkspaceSnapshot": {
        "typescript": "HubAgentWorkspace",
        "python": "AgentWorkspaceSnapshot",
    },
    "HubState": {"typescript": "HubData", "python": "HubState"},
}


def _load_schema(schema_path: Path) -> Dict[str, Dict[str, object]]:
    return load_json_document(schema_path, label="Schema")


def _find_matching_brace(text: str, start: int) -> int:
    depth = 0
    for idx in range(start, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return idx
    raise ValueError("Unmatched brace in TypeScript interface")


def _extract_ts_interfaces(ts_path: Path) -> Dict[str, Set[str]]:
    content = load_text_document(ts_path, label="TypeScript interface file")
    interfaces: Dict[str, Set[str]] = {}
    for match in re.finditer(r"\binterface\s+(\w+)\s*{", content):
        name = match.group(1)
        start = match.end() - 1
        end = _find_matching_brace(content, start)
        body = content[start + 1 : end]
        props: Set[str] = set()
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            prop_match = re.match(r"([A-Za-z0-9_]+)\s*\??\s*:", stripped)
            if prop_match:
                props.add(prop_match.group(1))
        interfaces[name] = props
    return interfaces


def _decorator_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if _decorator_name(decorator) == "dataclass":
            return True
    return False


def _extract_python_classes(
    py_path: Path,
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    content = load_text_document(py_path, label="Python interface file")
    tree = ast.parse(content)
    classes: Dict[str, Set[str]] = {}
    to_dict_keys: Dict[str, Set[str]] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass(node):
            continue
        fields: Set[str] = set()
        dict_keys: Set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.add(stmt.target.id)
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "to_dict":
                for child in ast.walk(stmt):
                    if isinstance(child, ast.Dict):
                        for key in child.keys:
                            if isinstance(key, ast.Constant) and isinstance(
                                key.value, str
                            ):
                                dict_keys.add(key.value)
        classes[node.name] = fields
        if dict_keys:
            to_dict_keys[node.name] = dict_keys
    return classes, to_dict_keys


def _normalize_schema_props(schema_def: Dict[str, object]) -> Tuple[Set[str], Set[str]]:
    props = set((schema_def.get("properties") or {}).keys())
    required = set(schema_def.get("required") or [])
    return props, required


def _diff_props(
    name: str,
    label: str,
    props: Set[str],
    required: Set[str],
    schema_props: Set[str],
) -> Iterable[str]:
    missing_required = required - props
    missing_optional = (schema_props - required) - props
    extra = props - schema_props
    for field in sorted(missing_required):
        yield f"{name}: {label} missing required property '{field}'"
    for field in sorted(missing_optional):
        yield f"{name}: {label} missing property '{field}'"
    for field in sorted(extra):
        yield f"{name}: {label} has extra property '{field}'"


def validate_interfaces(
    schema_path: Path, ts_path: Path, py_path: Path
) -> Tuple[bool, list[str]]:
    schema = _load_schema(schema_path)
    ts_interfaces = _extract_ts_interfaces(ts_path)
    py_classes, py_to_dict = _extract_python_classes(py_path)

    errors: list[str] = []

    for schema_name, schema_def in schema.items():
        schema_props, required = _normalize_schema_props(schema_def)
        binding = SCHEMA_BINDINGS.get(schema_name, {})
        ts_name = binding.get("typescript", schema_name)
        py_name = binding.get("python", schema_name)
        py_extra = set(binding.get("python_extra", set()))

        ts_props = ts_interfaces.get(ts_name)
        if ts_props is None:
            errors.append(f"{schema_name}: missing TypeScript interface '{ts_name}'")
        else:
            errors.extend(
                _diff_props(
                    schema_name,
                    f"TypeScript {ts_name}",
                    ts_props,
                    required,
                    schema_props,
                )
            )

        py_props = py_to_dict.get(py_name) or py_classes.get(py_name)
        if py_props is None:
            errors.append(f"{schema_name}: missing Python dataclass '{py_name}'")
        else:
            merged = set(py_props) | py_extra
            errors.extend(
                _diff_props(
                    schema_name, f"Python {py_name}", merged, required, schema_props
                )
            )

    return not errors, errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate hub TypeScript/Python interface contracts."
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("schemas/hub.json"),
        help="Path to shared schema JSON.",
    )
    parser.add_argument(
        "--typescript",
        type=Path,
        default=Path("src/codex_autorunner/static_src/hubTypes.ts"),
        help="Path to TypeScript hub interface file.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path("src/codex_autorunner/core/hub_topology.py"),
        help="Path to Python hub module.",
    )
    args = parser.parse_args()

    try:
        ok, errors = validate_interfaces(args.schema, args.typescript, args.python)
    except (FileNotFoundError, ValueError, SyntaxError) as exc:
        return emit_cli_report(
            success_message="✓ Hub interface contracts match schema",
            issues=[str(exc)],
            failure_stream=sys.stderr,
            bullet_prefix="✗ ",
        )

    return emit_cli_report(
        success_message="✓ Hub interface contracts match schema",
        issues=[] if ok else errors,
        failure_stream=sys.stderr,
        bullet_prefix="✗ ",
    )


if __name__ == "__main__":
    raise SystemExit(main())
