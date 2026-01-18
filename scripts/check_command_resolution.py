#!/usr/bin/env python3
"""Fail if we use shutil.which directly outside approved modules."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "codex_autorunner"
ALLOWED_SHUTIL_WHICH = {
    SRC_ROOT / "core" / "utils.py",
    SRC_ROOT / "core" / "update.py",
}


@dataclass
class WhichUsage:
    path: Path
    line: int
    column: int


def _iter_python_files() -> Iterable[Path]:
    for path in SRC_ROOT.rglob("*.py"):
        yield path


def _collect_shutil_which(path: Path) -> list[WhichUsage]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    shutil_aliases: set[str] = set()
    which_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "shutil":
                    shutil_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module != "shutil":
                continue
            for alias in node.names:
                if alias.name == "which":
                    which_aliases.add(alias.asname or alias.name)

    usages: list[WhichUsage] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if (
                isinstance(func.value, ast.Name)
                and func.value.id in shutil_aliases
                and func.attr == "which"
            ):
                usages.append(
                    WhichUsage(
                        path=path,
                        line=node.lineno,
                        column=node.col_offset,
                    )
                )
        elif isinstance(func, ast.Name):
            if func.id in which_aliases:
                usages.append(
                    WhichUsage(
                        path=path,
                        line=node.lineno,
                        column=node.col_offset,
                    )
                )
    return usages


def main() -> int:
    violations: list[WhichUsage] = []
    for path in _iter_python_files():
        if path in ALLOWED_SHUTIL_WHICH:
            continue
        violations.extend(_collect_shutil_which(path))

    if not violations:
        return 0

    print("shutil.which usage is restricted; use resolve_executable instead.", file=sys.stderr)
    for usage in violations:
        rel_path = usage.path.relative_to(REPO_ROOT)
        print(f"- {rel_path}:{usage.line}:{usage.column}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
