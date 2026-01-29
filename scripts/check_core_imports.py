#!/usr/bin/env python3
"""
Check that core/* does not import from integrations/* or agents/* implementations.

This enforces one-way dependencies: adapters can import core, but core cannot
import adapter implementations. Core should only depend on the AgentBackend/RunEvent
interfaces defined in integrations/agents/.
"""

import ast
import sys
from pathlib import Path
from typing import Set, Tuple


def is_inside_type_checking(
    node: ast.AST, parent_map: dict[ast.AST, ast.AST | None]
) -> bool:
    """
    Check if a node is inside a TYPE_CHECKING block.

    This allows imports inside `if TYPE_CHECKING:` blocks which are only used for
    type annotations and don't create runtime dependencies.
    """
    current = node
    while current is not None:
        parent = parent_map.get(current)
        if isinstance(parent, ast.If):
            # Check if this is an `if TYPE_CHECKING:` block
            if isinstance(parent.test, ast.Name) and parent.test.id == "TYPE_CHECKING":
                return True
        current = parent
    return False


def build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST | None]:
    """Build a map from each AST node to its parent node."""
    parent_map: dict[ast.AST, ast.AST | None] = {}

    def build(n: ast.AST, parent: ast.AST | None = None):
        parent_map[n] = parent
        for child in ast.iter_child_nodes(n):
            build(child, n)

    build(tree)
    return parent_map


def get_imports(
    filepath: Path, package_root: Path
) -> Set[Tuple[str, int, tuple[str, ...]]]:
    """
    Extract all import statements from a Python file and convert relative imports to absolute.
    Imports inside TYPE_CHECKING blocks are excluded.

    Returns a set of (module, lineno, imported_names) tuples.
    imported_names is a tuple of the names being imported from the module.
    """
    imports = set()

    # Determine the package of the file based on its location under src/codex_autorunner
    try:
        src_dir = package_root / "src" / "codex_autorunner"
        rel_path = filepath.relative_to(src_dir)
        parts = list(rel_path.parts)
        # Remove the filename
        if parts and parts[-1].endswith(".py"):
            parts = parts[:-1]
        file_package = (
            "codex_autorunner." + ".".join(parts) if parts else "codex_autorunner"
        )
    except ValueError:
        file_package = "codex_autorunner"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(filepath))
    except Exception:
        return imports

    # Build parent map once for efficiency
    parent_map = build_parent_map(tree)

    for node in ast.walk(tree):
        # Skip imports inside TYPE_CHECKING blocks
        if is_inside_type_checking(node, parent_map):
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add((alias.name, node.lineno, ()))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Convert relative import to absolute
                module = node.module
                level = node.level

                if level > 0:
                    # Calculate the parent package based on level
                    # The AST 'level' is the number of dots, so go up (level - 1) package levels
                    # For a file in package "a.b.c":
                    #   - from .x (level=1) means "a.b.c.x"
                    #   - from ..x (level=2) means "a.x"
                    #   - from ...x (level=3) means "x"
                    base_parts = file_package.split(".")
                    levels_up = level - 1  # Convert dots to package levels to go up
                    if levels_up <= len(base_parts):
                        # Go up 'levels_up' package levels
                        new_len = len(base_parts) - levels_up
                        base_parts = base_parts[:new_len] if new_len > 0 else []
                        parent_package = ".".join(base_parts)
                        # Prepend the parent package to get the absolute module name
                        module = (
                            parent_package + "." + module if parent_package else module
                        )
                    else:
                        # Going above the base package, skip this import
                        continue

                for alias in node.names:
                    imports.add((module, node.lineno, (alias.name,)))

    return imports


def is_forbidden_import(
    module: str, imported_names: tuple[str, ...]
) -> Tuple[bool, str]:
    """
    Check if an import is forbidden (core importing from adapter implementations).

    Returns (is_forbidden, reason).
    """
    # Core can import from integrations/agents interfaces
    if module.startswith("codex_autorunner.integrations.agents"):
        # Check if it's importing from interface files only
        allowed_interfaces = {
            "agent_backend",
            "run_event",
        }
        # Allowed interface symbols that core can import from integrations/agents package
        allowed_interface_symbols = {
            "AgentBackend",
            "AgentEvent",
            "AgentEventType",
            "RunEvent",
            "Started",
            "OutputDelta",
            "ToolCall",
            "ApprovalRequested",
            "Completed",
            "Failed",
        }
        parts = module.split(".")
        if len(parts) >= 4:
            # codex_autorunner.integrations.agents.<something>
            impl_module = parts[3]
            if impl_module in allowed_interfaces:
                return False, ""
            return (
                True,
                f"forbidden import from integrations/agents/{impl_module} (implementation)",
            )
        elif module == "codex_autorunner.integrations.agents":
            # Package-level import: only allow interface symbols explicitly.
            if not imported_names:
                return (
                    True,
                    "forbidden import from integrations/agents package (imported module)",
                )
            forbidden_names = [
                name for name in imported_names if name not in allowed_interface_symbols
            ]
            if forbidden_names:
                return (
                    True,
                    "forbidden import from integrations/agents "
                    f"(implementation symbols: {', '.join(forbidden_names)})",
                )
        return False, ""

    # Core cannot import from integrations/app_server implementations
    if module.startswith("codex_autorunner.integrations.app_server"):
        return True, "forbidden import from integrations/app_server (implementation)"

    # Core cannot import from agents implementations
    if module.startswith("codex_autorunner.agents"):
        return True, "forbidden import from agents (implementation)"

    return False, ""


def check_core_file(filepath: Path, core_dir: Path, package_root: Path) -> list[str]:
    """Check a single core file for forbidden imports."""
    errors = []

    try:
        imports = get_imports(filepath, package_root)
        for module, lineno, imported_names in imports:
            is_forbidden, reason = is_forbidden_import(module, imported_names)
            if is_forbidden:
                rel_path = filepath.relative_to(core_dir.parent)
                errors.append(f"{rel_path}:{lineno}: {reason} (from '{module}')")
    except Exception as e:
        errors.append(f"{filepath}: failed to parse: {e}")

    return errors


def load_allowlist(path: Path) -> set[tuple[str, str]]:
    """Load allowlist from JSON file."""
    import json

    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text())
        entries = set()
        for item in payload.get("violations", []):
            importer = item.get("importer")
            imported = item.get("imported")
            if importer and imported:
                entries.add((importer, imported))
        return entries
    except Exception:
        return set()


def main():
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    src_dir = repo_root / "src"
    core_dir = src_dir / "codex_autorunner" / "core"

    if not core_dir.exists():
        print(f"Error: core directory not found: {core_dir}")
        sys.exit(1)

    all_errors = []

    # Load allowlist
    allowlist_path = repo_root / "scripts" / "import_boundaries_allowlist.json"
    allowlist = load_allowlist(allowlist_path)

    # Check all Python files in core/
    for py_file in core_dir.rglob("*.py"):
        errors = check_core_file(py_file, core_dir, repo_root)
        all_errors.extend(errors)

    # Filter out allowlisted violations
    unallowlisted = []
    for error in all_errors:
        # Parse error format: "path:line: reason (from 'module')"
        parts = error.split(":", 1)
        if len(parts) == 2:
            rel_path = parts[0]
            # Extract module name from the error
            if "(from '" in error and "')" in error:
                start = error.find("(from '") + 7
                end = error.rfind("')")
                module = error[start:end] if start > 0 and end > start else ""
                if module and (rel_path, module) in allowlist:
                    continue
        unallowlisted.append(error)

    if unallowlisted:
        print("Error: core/ files have forbidden imports from adapter implementations:")
        for error in sorted(unallowlisted):
            print(f"  {error}")
        print("\nCore should only import from integrations/agents/agent_backend.py")
        print("and integrations/agents/run_event.py (the interface definitions).")
        sys.exit(1)

    print("OK: No forbidden imports found in core/")
    sys.exit(0)


if __name__ == "__main__":
    main()
