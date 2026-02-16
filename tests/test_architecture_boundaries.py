"""Architecture boundary enforcement test.

Scans Python source files using AST to enforce one-way dependencies:
    Surfaces -> Adapters -> Control Plane -> Engine

Layers are defined by module prefix:
- Engine: codex_autorunner.core.flows*, codex_autorunner.core.ports*
- Control Plane: codex_autorunner.core* (excluding flows/ports),
                 codex_autorunner.contextspace*, codex_autorunner.tickets*
- Adapters: codex_autorunner.integrations*, codex_autorunner.agents*
- Surfaces: codex_autorunner.surfaces*

Top-level modules under ``codex_autorunner.<name>`` that do not match a
package-prefix rule default to CONTROL_PLANE (for example: ``manifest``,
``server``, ``bootstrap``). This prevents "unknown layer" escapes.

Shim modules (*_shim.py) are allowed to break rules but must declare reason
in a comment header containing "ARCHITECTURE_SHIM:".
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parent.parent / "src" / "codex_autorunner"


class Layer(IntEnum):
    ENGINE = 0
    CONTROL_PLANE = 1
    ADAPTERS = 2
    SURFACES = 3
    UNKNOWN = 99


@dataclass
class ModuleInfo:
    path: Path
    module_name: str
    layer: Layer
    is_shim: bool


@dataclass
class Violation:
    source_path: Path
    source_module: str
    source_layer: Layer
    imported_module: str
    imported_layer: Layer
    allowed_direction: str


LAYER_PREFIXES: dict[Layer, list[str]] = {
    Layer.ENGINE: [
        "codex_autorunner.core.flows",
        "codex_autorunner.core.ports",
    ],
    Layer.CONTROL_PLANE: [
        "codex_autorunner.core",
        "codex_autorunner.contextspace",
        "codex_autorunner.tickets",
    ],
    Layer.ADAPTERS: [
        "codex_autorunner.integrations",
        "codex_autorunner.agents",
    ],
    Layer.SURFACES: [
        "codex_autorunner.surfaces",
    ],
}

SHIM_PATTERN = re.compile(r"_shim\.py$|_shim/__init__\.py$")
SHIM_DECLARATION_PATTERN = re.compile(r"ARCHITECTURE_SHIM:")


def module_name_from_path(path: Path) -> str:
    parts = list(path.parts)
    try:
        src_idx = parts.index("src")
    except ValueError:
        return ""
    module_parts = parts[src_idx + 1 :]
    if module_parts and module_parts[-1] == "__init__.py":
        module_parts = module_parts[:-1]
    elif module_parts and module_parts[-1].endswith(".py"):
        module_parts[-1] = module_parts[-1][:-3]
    return ".".join(module_parts)


def classify_module(module_name: str) -> Layer:
    if not module_name or not module_name.startswith("codex_autorunner."):
        return Layer.UNKNOWN

    for layer in [Layer.ENGINE, Layer.CONTROL_PLANE, Layer.ADAPTERS, Layer.SURFACES]:
        for prefix in LAYER_PREFIXES[layer]:
            if module_name == prefix or module_name.startswith(prefix + "."):
                if layer == Layer.CONTROL_PLANE:
                    for engine_prefix in LAYER_PREFIXES[Layer.ENGINE]:
                        if module_name == engine_prefix or module_name.startswith(
                            engine_prefix + "."
                        ):
                            return Layer.ENGINE
                return layer
    # Harden boundary checks: classify unmatched top-level modules as
    # Control Plane so imports like codex_autorunner.manifest are enforced.
    parts = module_name.split(".")
    if len(parts) == 2 and parts[0] == "codex_autorunner":
        return Layer.CONTROL_PLANE
    return Layer.UNKNOWN


def is_shim_file(path: Path) -> bool:
    if SHIM_PATTERN.search(path.name):
        return True
    if path.name == "__init__.py":
        if SHIM_PATTERN.search(path.parent.name + "/__init__.py"):
            return True
    return False


def has_shim_declaration(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return bool(SHIM_DECLARATION_PATTERN.search(content))
    except OSError:
        return False


def extract_imports(source: str) -> list[str]:
    imports = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def collect_python_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*.py"):
        if any(
            part.startswith(".") or part.startswith("__pycache__")
            for part in path.parts
        ):
            continue
        files.append(path)
    return sorted(files)


def check_violations(files: list[Path]) -> list[Violation]:
    violations = []

    for path in files:
        module_name = module_name_from_path(path)
        if not module_name:
            continue

        source_layer = classify_module(module_name)
        if source_layer == Layer.UNKNOWN:
            continue

        is_shim = is_shim_file(path) and has_shim_declaration(path)
        if is_shim:
            continue

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        imports = extract_imports(source)

        for imported in imports:
            if not imported.startswith("codex_autorunner."):
                continue

            imported_layer = classify_module(imported)
            if imported_layer == Layer.UNKNOWN:
                continue

            if imported_layer > source_layer:
                allowed = f"{imported_layer.name} -> {source_layer.name}"
                violations.append(
                    Violation(
                        source_path=path,
                        source_module=module_name,
                        source_layer=source_layer,
                        imported_module=imported,
                        imported_layer=imported_layer,
                        allowed_direction=allowed,
                    )
                )

    return violations


@pytest.mark.slow
def test_architecture_boundaries():
    if not SRC_ROOT.exists():
        pytest.skip(f"Source root not found: {SRC_ROOT}")

    files = collect_python_files(SRC_ROOT)
    violations = check_violations(files)

    if violations:
        lines = ["Architecture boundary violations detected:\n"]
        lines.append(
            "Allowed dependency direction: Surfaces -> Adapters -> Control Plane -> Engine\n"
        )
        lines.append("Reverse dependencies are forbidden.\n")
        lines.append("-" * 80 + "\n")

        for v in violations:
            lines.append(
                f"VIOLATION: {v.source_path.relative_to(SRC_ROOT.parent.parent)}\n"
                f"  Layer: {v.source_layer.name}\n"
                f"  Forbidden import: {v.imported_module} ({v.imported_layer.name})\n"
                f"  Allowed direction: {v.allowed_direction}\n"
                f"  \n"
            )

        lines.append("-" * 80 + "\n")
        lines.append("To fix:\n")
        lines.append("  1. Refactor to inject dependencies via constructor or config\n")
        lines.append("  2. Move shared code to a lower layer\n")
        lines.append(
            "  3. If truly necessary, create a *_shim.py with 'ARCHITECTURE_SHIM:' comment\n"
        )

        pytest.fail("\n".join(lines))


def test_core_runtime_does_not_import_web_modules(monkeypatch):
    import importlib
    import sys

    for name in list(sys.modules):
        if name.startswith("codex_autorunner.surfaces.web"):
            sys.modules.pop(name, None)
        if name.startswith("codex_autorunner.web"):
            sys.modules.pop(name, None)

    importlib.invalidate_caches()

    import codex_autorunner.core.runtime  # noqa: F401

    leaked = [
        name
        for name in sys.modules
        if name.startswith("codex_autorunner.surfaces.web")
        or name.startswith("codex_autorunner.web")
    ]
    assert (
        not leaked
    ), f"core.runtime should not import web/surfaces modules, found {leaked}"


def test_engine_does_not_import_control_plane():
    engine_files = collect_python_files(
        SRC_ROOT / "core" / "flows"
    ) + collect_python_files(SRC_ROOT / "core" / "ports")

    violations = []
    for path in engine_files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        imports = extract_imports(source)
        for imported in imports:
            if imported.startswith("codex_autorunner.core."):
                if imported.startswith(
                    "codex_autorunner.core.flows."
                ) or imported.startswith("codex_autorunner.core.ports."):
                    continue
                if (
                    imported == "codex_autorunner.core.flows"
                    or imported == "codex_autorunner.core.ports"
                ):
                    continue
                violations.append(
                    f"{path.relative_to(SRC_ROOT.parent.parent)} imports {imported}"
                )
            elif imported.startswith("codex_autorunner.contextspace"):
                violations.append(
                    f"{path.relative_to(SRC_ROOT.parent.parent)} imports {imported}"
                )
            elif imported.startswith("codex_autorunner.tickets"):
                violations.append(
                    f"{path.relative_to(SRC_ROOT.parent.parent)} imports {imported}"
                )

    assert (
        not violations
    ), "Engine modules should not import Control Plane:\n" + "\n".join(violations)


def _engine_flows_forbidden_imports_for_module(
    module_name: str, imports: list[str]
) -> list[str]:
    """Return forbidden codex_autorunner imports for a core.flows module."""
    if not (
        module_name.startswith("codex_autorunner.core.flows.")
        or module_name == "codex_autorunner.core.flows"
    ):
        return []
    violations: list[str] = []
    for imported in imports:
        if not imported.startswith("codex_autorunner."):
            continue
        if imported == "codex_autorunner.core.flows" or imported.startswith(
            "codex_autorunner.core.flows."
        ):
            continue
        if imported == "codex_autorunner.core.ports" or imported.startswith(
            "codex_autorunner.core.ports."
        ):
            continue
        violations.append(imported)
    return violations


def test_engine_flows_import_scope_is_restricted() -> None:
    flow_files = collect_python_files(SRC_ROOT / "core" / "flows")
    violations: list[str] = []
    for path in flow_files:
        module_name = module_name_from_path(path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        imports = extract_imports(source)
        bad = _engine_flows_forbidden_imports_for_module(module_name, imports)
        violations.extend(
            f"{path.relative_to(SRC_ROOT.parent.parent)} imports {imported}"
            for imported in bad
        )

    assert not violations, (
        "core/flows modules may import only core/flows, core/ports, or external libs:\n"
        + "\n".join(violations)
    )


def test_engine_regression_detects_top_level_control_plane_import() -> None:
    module_name = "codex_autorunner.core.flows.worker_process"
    imports = [
        "typing",
        "codex_autorunner.core.flows.models",
        "codex_autorunner.manifest",
    ]
    violations = _engine_flows_forbidden_imports_for_module(module_name, imports)
    assert violations == ["codex_autorunner.manifest"]


def test_top_level_module_is_not_unknown_layer() -> None:
    assert classify_module("codex_autorunner.manifest") == Layer.CONTROL_PLANE
