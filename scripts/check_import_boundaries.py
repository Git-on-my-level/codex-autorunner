#!/usr/bin/env python3
"""Import boundary checker to ensure core/ doesn't import from integrations/ or surfaces/."""

import ast
import sys
from pathlib import Path
from typing import Set


def find_imports(file_path: Path) -> Set[str]:
    """Extract all import statements from a Python file."""
    imports = set()

    try:
        with open(file_path, "r") as f:
            content = f.read()
            tree = ast.parse(content, filename=str(file_path))

        type_checking_blocks = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                    for sub_node in ast.walk(node):
                        if isinstance(sub_node, (ast.ImportFrom, ast.Import)):
                            type_checking_blocks.add(sub_node)

        for node in ast.walk(tree):
            if node in type_checking_blocks:
                continue
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.add(module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
    except Exception:
        pass

    return imports


def check_core_boundaries(root_dir: Path) -> list[tuple[str, str]]:
    """Check that core/ modules don't import from integrations/ or surfaces/."""
    violations = []
    core_dir = root_dir / "src" / "codex_autorunner" / "core"

    if not core_dir.exists():
        return violations

    for py_file in core_dir.rglob("*.py"):
        imports = find_imports(py_file)

        for imp in imports:
            if imp.startswith("integrations."):
                violations.append((str(py_file.relative_to(root_dir)), imp))
            elif imp.startswith("surfaces."):
                violations.append((str(py_file.relative_to(root_dir)), imp))

    return violations


def main():
    root_dir = Path(__file__).parent.parent
    violations = check_core_boundaries(root_dir)

    if violations:
        print("Import boundary violations detected:")
        for file_path, imp in violations:
            print(f"  {file_path} imports {imp}")
        print("\nCore modules must not import from integrations/ or surfaces/")
        sys.exit(1)
    else:
        print("Import boundary check passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
