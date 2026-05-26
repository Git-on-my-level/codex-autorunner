"""Architecture boundary enforcement test.

Scans Python source files using AST to enforce one-way dependencies:
    Surfaces -> Adapters -> Control Plane -> Engine

Layers are defined by module prefix:
- Engine: codex_autorunner.core.flows*, codex_autorunner.core.ports*
- Control Plane: codex_autorunner.core* (excluding flows/ports),
                 codex_autorunner.contextspace*, codex_autorunner.tickets*
- Adapters: codex_autorunner.adapters*, codex_autorunner.agents*
- Adapter composition: codex_autorunner.flows*
- Surfaces: codex_autorunner.surfaces*

Top-level modules under ``codex_autorunner.<name>`` that do not match a
package-prefix rule default to CONTROL_PLANE (for example: ``manifest``,
``server``, ``bootstrap``). This prevents "unknown layer" escapes.

Shim modules (*_shim.py) are allowed to break rules but must declare reason
in a comment header containing "ARCHITECTURE_SHIM:".
"""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Iterable
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


@dataclass(frozen=True)
class PmaRouteBoundaryException:
    importer: str
    imported: str
    reason: str
    removal_condition: str


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
        "codex_autorunner.adapters",
        "codex_autorunner.agents",
        "codex_autorunner.flows",
    ],
    Layer.SURFACES: [
        "codex_autorunner.surfaces",
    ],
}

PMA_ROUTE_PRIVATE_PREFIX = "codex_autorunner.surfaces.web.routes.pma_routes"
PMA_ROUTE_BOUNDARY_EXCEPTION_BY_IMPORT: dict[
    tuple[str, str], PmaRouteBoundaryException
] = {
    (
        "codex_autorunner.surfaces.web.app",
        "codex_autorunner.surfaces.web.routes.pma_routes",
    ): PmaRouteBoundaryException(
        importer="codex_autorunner.surfaces.web.app",
        imported="codex_autorunner.surfaces.web.routes.pma_routes",
        reason=(
            "Hub app wiring creates the PMA container runtime state while binding "
            "the PMA router."
        ),
        removal_condition=(
            "Remove when PmaRuntimeState is owned by the PMA application "
            "container/service instead of the route package."
        ),
    ),
}

SHIM_PATTERN = re.compile(r"_shim\.py$|_shim/__init__\.py$")
SHIM_DECLARATION_PATTERN = re.compile(r"ARCHITECTURE_SHIM:")

ALLOWED_BOUNDARY_IMPORTS: dict[str, set[str]] = {
    # Existing package-root compatibility facades.
    "codex_autorunner.api": {
        "codex_autorunner.agents.base",
        "codex_autorunner.agents.registry",
        "codex_autorunner.agents.types",
    },
    "codex_autorunner.cli": {
        "codex_autorunner.surfaces.cli.cli",
    },
    "codex_autorunner.housekeeping": {
        "codex_autorunner.adapters.docker.runtime",
    },
    "codex_autorunner.pma_chat_delivery_runtime": {
        "codex_autorunner.adapters.chat.bound_live_progress",
        "codex_autorunner.adapters.chat.pma_delivery",
        "codex_autorunner.adapters.discord.pma_delivery",
        "codex_autorunner.adapters.discord.state",
        "codex_autorunner.adapters.telegram.pma_delivery",
        "codex_autorunner.adapters.telegram.state",
    },
    "codex_autorunner.server": {
        "codex_autorunner.surfaces.web.app",
        "codex_autorunner.surfaces.web.middleware",
    },
    "codex_autorunner.ticket_helper_script_common": {
        "codex_autorunner.agents.registry",
    },
    # Existing ticket/control-plane helpers that validate configured agent IDs.
    "codex_autorunner.tickets.bulk": {
        "codex_autorunner.agents.hermes_identity",
    },
    "codex_autorunner.tickets.lint": {
        "codex_autorunner.agents.hermes_identity",
        "codex_autorunner.agents.registry",
    },
    "codex_autorunner.tickets.pack_import": {
        "codex_autorunner.agents.registry",
    },
    "codex_autorunner.tickets.runner": {
        "codex_autorunner.agents.hermes_identity",
    },
    "codex_autorunner.tickets.runner_selection": {
        "codex_autorunner.agents.registry",
    },
    # Existing flow-engine/control-plane crossings exposed by resolving relative
    # imports. They are kept explicit so new relative crossings still fail.
    "codex_autorunner.core.flows.app_server_event_compaction": {
        "codex_autorunner.core.text_utils",
    },
    "codex_autorunner.core.flows.archive_helpers": {
        "codex_autorunner.bootstrap",
        "codex_autorunner.core.apps.artifacts",
        "codex_autorunner.core.apps.hooks",
        "codex_autorunner.core.archive",
        "codex_autorunner.core.archive_retention",
        "codex_autorunner.core.config",
        "codex_autorunner.core.managed_thread_store",
        "codex_autorunner.core.sqlite_utils",
        "codex_autorunner.core.state_lifecycle",
        "codex_autorunner.core.state_roots",
        "codex_autorunner.manifest",
        "codex_autorunner.tickets.files",
        "codex_autorunner.tickets.outbox",
    },
    "codex_autorunner.core.flows.controller": {
        "codex_autorunner.core.git_utils",
        "codex_autorunner.core.lifecycle_events",
        "codex_autorunner.core.state_roots",
        "codex_autorunner.core.utils",
        "codex_autorunner.manifest",
    },
    "codex_autorunner.core.flows.failure_diagnostics": {
        "codex_autorunner.core.coercion",
    },
    "codex_autorunner.core.flows.flow_housekeeping": {
        "codex_autorunner.core.config",
    },
    "codex_autorunner.core.flows.flow_telemetry_hooks": {
        "codex_autorunner.core.apps.hooks",
        "codex_autorunner.core.config",
        "codex_autorunner.core.state_roots",
    },
    "codex_autorunner.core.flows.hub_overview": {
        "codex_autorunner.core.chat_bindings",
        "codex_autorunner.core.state_roots",
        "codex_autorunner.manifest",
    },
    "codex_autorunner.core.flows.pause_dispatch": {
        "codex_autorunner.core.redaction",
        "codex_autorunner.core.ticket_flow_projection",
        "codex_autorunner.tickets.outbox",
    },
    "codex_autorunner.core.flows.reconciler": {
        "codex_autorunner.core.config",
        "codex_autorunner.core.locks",
        "codex_autorunner.core.state_roots",
        "codex_autorunner.tickets.outbox",
        "codex_autorunner.tickets.replies",
    },
    "codex_autorunner.core.flows.runtime": {
        "codex_autorunner.core.lifecycle_events",
    },
    "codex_autorunner.core.flows.start_policy": {
        "codex_autorunner.tickets.files",
        "codex_autorunner.tickets.ingest_state",
        "codex_autorunner.tickets.lint",
    },
    "codex_autorunner.core.flows.store": {
        "codex_autorunner.core.config",
        "codex_autorunner.core.sqlite_utils",
        "codex_autorunner.core.state_roots",
        "codex_autorunner.core.time_utils",
    },
    "codex_autorunner.core.flows.telemetry_export": {
        "codex_autorunner.core.state_lifecycle",
    },
    "codex_autorunner.core.flows.ux_helpers": {
        "codex_autorunner.core.freshness",
        "codex_autorunner.core.ticket_flow_operator",
        "codex_autorunner.core.ticket_flow_summary",
        "codex_autorunner.tickets.files",
    },
    "codex_autorunner.core.flows.worker_process": {
        "codex_autorunner.core.text_utils",
        "codex_autorunner.core.utils",
    },
    "codex_autorunner.core.flows.worker_reaper": {
        "codex_autorunner.core.diagnostics.process_snapshot",
        "codex_autorunner.core.state_roots",
        "codex_autorunner.core.text_utils",
        "codex_autorunner.flow_worker_reaper_constants",
    },
    "codex_autorunner.core.flows.workspace_root": {
        "codex_autorunner.core.utils",
    },
    "codex_autorunner.core.ports.agent_backend": {
        "codex_autorunner.core.time_utils",
    },
    "codex_autorunner.core.ports.memory_store": {
        "codex_autorunner.core.domain.refs",
    },
    "codex_autorunner.core.ports.run_event": {
        "codex_autorunner.core.time_utils",
    },
    "codex_autorunner.core.ports.scope_resolver": {
        "codex_autorunner.core.domain.refs",
    },
    "codex_autorunner.core.ports.surface_port": {
        "codex_autorunner.core.domain.refs",
    },
    "codex_autorunner.core.ports.thread_store": {
        "codex_autorunner.core.domain.refs",
    },
    "codex_autorunner.core.ports.ticket_store": {
        "codex_autorunner.core.domain.refs",
    },
    # Explicit composition seam: the orchestration control-plane exposes ticket
    # flow targets while the concrete ticket-flow runtime lives in flows/.
    "codex_autorunner.core.orchestration.flows": {
        "codex_autorunner.flows.ticket_flow.runtime_helpers",
    },
    "codex_autorunner.core.automation.ticket_flow_executor": {
        "codex_autorunner.flows.ticket_flow.runtime_helpers",
    },
    "codex_autorunner.core.pma_inbox": {
        "codex_autorunner.flows.ticket_flow.runtime_helpers",
    },
}


def is_allowed_boundary_import(source_module: str, imported: str) -> bool:
    return imported in ALLOWED_BOUNDARY_IMPORTS.get(source_module, set())


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


def _source_package_name(source_module: str, source_path: Path | None) -> str:
    if source_path is not None and source_path.name == "__init__.py":
        return source_module
    return source_module.rpartition(".")[0]


def _resolve_import_from_module(
    node: ast.ImportFrom,
    *,
    source_module: str,
    source_path: Path | None,
) -> list[str]:
    if node.level == 0:
        return [node.module] if node.module else []

    package_name = _source_package_name(source_module, source_path)
    if not package_name:
        return []

    relative_name = "." * node.level + (node.module or "")
    try:
        resolved = importlib.util.resolve_name(relative_name, package_name)
    except ImportError:
        return []

    if node.module:
        return [resolved]
    return [f"{resolved}.{alias.name}" for alias in node.names]


def extract_imports(
    source: str,
    *,
    source_module: str = "",
    source_path: Path | None = None,
) -> list[str]:
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
            imports.extend(
                _resolve_import_from_module(
                    node,
                    source_module=source_module,
                    source_path=source_path,
                )
            )
    return imports


def extract_imports_with_lines(
    source: str,
    *,
    source_module: str = "",
    source_path: Path | None = None,
) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for imported in _resolve_import_from_module(
                node,
                source_module=source_module,
                source_path=source_path,
            ):
                imports.append((imported, node.lineno))
    return imports


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _root_arg_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        return node.attr
    if isinstance(node, ast.Call) and _call_name(node.func) == "Path" and node.args:
        return _root_arg_name(node.args[0])
    return None


def _iter_calls(source: str) -> Iterable[ast.Call]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    return (node for node in ast.walk(tree) if isinstance(node, ast.Call))


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

        imports = extract_imports(
            source,
            source_module=module_name,
            source_path=path,
        )

        for imported in imports:
            if not imported.startswith("codex_autorunner."):
                continue

            imported_layer = classify_module(imported)
            if imported_layer == Layer.UNKNOWN:
                continue

            if is_allowed_boundary_import(module_name, imported):
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

    to_remove = [
        name for name in sys.modules if name.startswith("codex_autorunner.surfaces.web")
    ]
    saved = {name: sys.modules.pop(name) for name in to_remove}
    try:
        importlib.invalidate_caches()

        import codex_autorunner.core.runtime  # noqa: F401

        leaked = [
            name
            for name in sys.modules
            if name.startswith("codex_autorunner.surfaces.web")
        ]
        assert (
            not leaked
        ), f"core.runtime should not import web/surfaces modules, found {leaked}"
    finally:
        sys.modules.update(saved)


def _pma_route_boundary_guard_files() -> list[Path]:
    scoped_roots = [
        SRC_ROOT / "adapters",
        SRC_ROOT / "core",
        SRC_ROOT / "surfaces" / "cli",
        SRC_ROOT / "surfaces" / "web" / "services",
    ]
    files: list[Path] = []
    for root in scoped_roots:
        files.extend(collect_python_files(root))
    files.extend(
        path
        for path in [
            SRC_ROOT / "surfaces" / "web" / "app.py",
            SRC_ROOT / "surfaces" / "web" / "app_builders.py",
            SRC_ROOT / "surfaces" / "web" / "app_factory.py",
            SRC_ROOT / "surfaces" / "web" / "hub_jobs.py",
            SRC_ROOT / "surfaces" / "web" / "routes" / "scm_webhooks.py",
        ]
        if path.exists()
    )
    return sorted(set(files))


def _is_pma_route_boundary_exception(importer: str, imported: str) -> bool:
    exception = PMA_ROUTE_BOUNDARY_EXCEPTION_BY_IMPORT.get((importer, imported))
    return (
        exception is not None
        and exception.reason.strip()
        and exception.removal_condition.strip()
    )


def test_pma_route_private_helpers_do_not_leak_into_services_or_entrypoints() -> None:
    violations: list[str] = []
    for path in _pma_route_boundary_guard_files():
        module_name = module_name_from_path(path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        imports = extract_imports_with_lines(
            source,
            source_module=module_name,
            source_path=path,
        )
        for imported, line in imports:
            if imported != PMA_ROUTE_PRIVATE_PREFIX and not imported.startswith(
                f"{PMA_ROUTE_PRIVATE_PREFIX}."
            ):
                continue
            if _is_pma_route_boundary_exception(module_name, imported):
                continue
            violations.append(
                f"{path.relative_to(SRC_ROOT.parent.parent)}:{line} imports "
                f"{imported}; PMA routes may assemble services, but services, "
                "core, adapters, CLI hub commands, SCM webhooks, and web "
                "startup must not import route-private runtime/read-model helpers."
            )

    assert not violations, "\n".join(violations)


def test_pma_route_boundary_exceptions_are_documented_and_still_used() -> None:
    used: set[tuple[str, str]] = set()
    for path in _pma_route_boundary_guard_files():
        module_name = module_name_from_path(path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for imported, _line in extract_imports_with_lines(
            source,
            source_module=module_name,
            source_path=path,
        ):
            key = (module_name, imported)
            if key in PMA_ROUTE_BOUNDARY_EXCEPTION_BY_IMPORT:
                used.add(key)

    stale = set(PMA_ROUTE_BOUNDARY_EXCEPTION_BY_IMPORT) - used
    assert not stale, (
        "PMA route boundary exceptions are no longer used and should be removed:\n"
        + "\n".join(f"{importer} imports {imported}" for importer, imported in stale)
    )


# ---------------------------------------------------------------------------
# Runtime control-plane authority guardrails
# ---------------------------------------------------------------------------

_CONTROL_PLANE_HOT_ROOTS = (
    SRC_ROOT / "adapters" / "github",
    SRC_ROOT / "core" / "scm_automation_service.py",
    SRC_ROOT / "core" / "publish_executor.py",
    SRC_ROOT / "core" / "publish_journal.py",
    SRC_ROOT / "core" / "publish_operation_executors.py",
    SRC_ROOT / "core" / "publish_operation_factories.py",
    SRC_ROOT / "core" / "automation" / "executors.py",
    SRC_ROOT / "core" / "pma_queue.py",
    SRC_ROOT / "core" / "managed_thread_store.py",
    SRC_ROOT / "core" / "pr_bindings.py",
    SRC_ROOT / "core" / "scm_events.py",
    SRC_ROOT / "core" / "scm_polling_watches.py",
)

_CONTROL_PLANE_AUTHORITY_ALLOWLIST = {
    "adapters/github/scm_discovery.py",
}

_FORBIDDEN_AUTHORITY_RESOLVERS = {
    "find_hub_binding_context",
    "binding_context_from_root",
    "pr_binding_store_from_root",
}

_HOT_STORE_SELECTORS = {
    "open_orchestration_sqlite",
    "resolve_orchestration_sqlite_path",
    "PrBindingStore",
    "ScmEventStore",
    "ScmPollingWatchStore",
    "PublishJournalStore",
    "AutomationStore",
    "ManagedThreadStore",
    "PmaQueue",
}

_NON_AUTHORITY_ROOT_NAMES = {
    "checkout_root",
    "operation_checkout_root",
    "cwd",
    "repo_root",
    "repo_root_arg",
    "workspace_root",
    "worktree_root",
}


def _control_plane_hot_files() -> list[Path]:
    files: list[Path] = []
    for root in _CONTROL_PLANE_HOT_ROOTS:
        if root.is_dir():
            files.extend(collect_python_files(root))
        elif root.exists():
            files.append(root)
    return sorted(set(files))


def _file_key(path: Path) -> str:
    return str(path.relative_to(SRC_ROOT))


def test_hot_runtime_paths_do_not_import_parent_walk_authority_resolvers() -> None:
    violations: list[str] = []
    for path in _control_plane_hot_files():
        file_key = _file_key(path)
        if file_key in _CONTROL_PLANE_AUTHORITY_ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(".hub_binding_context"):
                        violations.append(
                            f"{file_key}:{node.lineno} imports {alias.name}; hot "
                            "runtime paths must receive hub_root explicitly instead "
                            "of rediscovering authority from a checkout path."
                        )
            elif isinstance(node, ast.ImportFrom):
                imported_names = {alias.name for alias in node.names}
                if not imported_names & _FORBIDDEN_AUTHORITY_RESOLVERS:
                    continue
                module = node.module or ""
                if module.endswith("pr_binding_runtime") or module.endswith(
                    "hub_binding_context"
                ):
                    forbidden = ", ".join(
                        sorted(imported_names & _FORBIDDEN_AUTHORITY_RESOLVERS)
                    )
                    violations.append(
                        f"{file_key}:{node.lineno} imports {forbidden} from "
                        f"{module}; hot runtime paths must receive hub_root "
                        "explicitly instead of rediscovering authority from a "
                        "checkout path."
                    )
                elif node.level > 0:
                    forbidden = ", ".join(
                        sorted(imported_names & _FORBIDDEN_AUTHORITY_RESOLVERS)
                    )
                    violations.append(
                        f"{file_key}:{node.lineno} imports {forbidden} via a "
                        "relative import; hot runtime paths must not import "
                        "parent-walk authority resolvers."
                    )

    assert not violations, "\n".join(violations)


def test_hot_runtime_paths_do_not_call_parent_walk_authority_resolvers() -> None:
    violations: list[str] = []
    for path in _control_plane_hot_files():
        file_key = _file_key(path)
        if file_key in _CONTROL_PLANE_AUTHORITY_ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for call in _iter_calls(source):
            name = _call_name(call.func)
            if name is None:
                continue
            if name.rsplit(".", 1)[-1] in _FORBIDDEN_AUTHORITY_RESOLVERS:
                violations.append(
                    f"{file_key}:{call.lineno} calls {name}; hot runtime paths "
                    "must not infer the owning hub from a repo/worktree path."
                )

    assert not violations, "\n".join(violations)


def test_hot_publish_and_enqueue_paths_do_not_select_stores_from_checkout_roots() -> (
    None
):
    scoped = [
        SRC_ROOT / "core" / "publish_operation_executors.py",
        SRC_ROOT / "core" / "publish_journal.py",
        SRC_ROOT / "core" / "publish_executor.py",
        SRC_ROOT / "core" / "scm_automation_service.py",
        SRC_ROOT / "core" / "automation" / "executors.py",
        SRC_ROOT / "adapters" / "github" / "publisher.py",
        SRC_ROOT / "adapters" / "github" / "polling.py",
    ]
    violations: list[str] = []
    for path in [item for item in scoped if item.exists()]:
        source = path.read_text(encoding="utf-8", errors="replace")
        file_key = _file_key(path)
        for call in _iter_calls(source):
            name = _call_name(call.func)
            if name is None or name.rsplit(".", 1)[-1] not in _HOT_STORE_SELECTORS:
                continue
            root_arg = call.args[0] if call.args else None
            if root_arg is None:
                continue
            arg_name = _root_arg_name(root_arg)
            if arg_name in _NON_AUTHORITY_ROOT_NAMES:
                violations.append(
                    f"{file_key}:{call.lineno} constructs {name} from {arg_name}; "
                    "publish/enqueue/provider stores must be selected by hub_root."
                )

    assert not violations, "\n".join(violations)


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

        module_name = module_name_from_path(path)
        imports = extract_imports(
            source,
            source_module=module_name,
            source_path=path,
        )
        for imported in imports:
            if is_allowed_boundary_import(module_name, imported):
                continue
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
        if is_allowed_boundary_import(module_name, imported):
            continue
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
        imports = extract_imports(
            source,
            source_module=module_name,
            source_path=path,
        )
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


def test_flows_modules_are_adapter_layer_composition_roots() -> None:
    assert classify_module("codex_autorunner.flows") == Layer.ADAPTERS
    assert classify_module("codex_autorunner.flows.ticket_flow.definition") == (
        Layer.ADAPTERS
    )


def test_extract_imports_resolves_relative_imports() -> None:
    source = (
        "from ...surfaces.web import app\n"
        "from .runtime_helpers import start_ticket_flow_run\n"
        "from . import models\n"
    )
    imports = extract_imports(
        source,
        source_module="codex_autorunner.core.flows.worker_process",
        source_path=SRC_ROOT / "core" / "flows" / "worker_process.py",
    )
    assert imports == [
        "codex_autorunner.surfaces.web",
        "codex_autorunner.core.flows.runtime_helpers",
        "codex_autorunner.core.flows.models",
    ]


def test_boundary_enforcement_catches_relative_reverse_import(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "codex_autorunner" / "core" / "flows"
    source_path.mkdir(parents=True)
    module_path = source_path / "worker_process.py"
    module_path.write_text("from ...surfaces.web import app\n", encoding="utf-8")

    violations = check_violations([module_path])

    assert len(violations) == 1
    assert violations[0].source_module == "codex_autorunner.core.flows.worker_process"
    assert violations[0].imported_module == "codex_autorunner.surfaces.web"
    assert violations[0].source_layer == Layer.ENGINE
    assert violations[0].imported_layer == Layer.SURFACES


# ---------------------------------------------------------------------------
# Side-process boundary regression tests
# ---------------------------------------------------------------------------

_SIDE_PROCESS_PREFIXES: tuple[str, ...] = (
    "codex_autorunner.adapters.discord",
    "codex_autorunner.adapters.telegram",
)

_FORBIDDEN_SHARED_STATE_PATTERNS: tuple[str, ...] = (
    "HubSupervisor",
    "open_orchestration_sqlite",
    "PmaAutomationStore",
    "PmaQueue",
    "ManagedThreadStore",
    "ScmPollingWatchStore",
)

_FORBIDDEN_NOTIFICATION_STORE_PATTERN = "PmaNotificationStore"

_FORBIDDEN_TRANSCRIPT_MIRROR_PATTERN = "TranscriptMirrorStore"

_FORBIDDEN_POLLING_OWNER_PATTERNS: tuple[str, ...] = (
    "HubLifecycleWorker",
    "GitHubScmPollingService",
    "build_hub_scm_poll_processor",
)

_SIDE_PROCESS_BOUNDARY_ALLOWLIST: dict[str, list[str]] = {
    "adapters/discord/service.py": [
        "build_ticket_flow_orchestration_service -- ALLOWED: ticket flow uses per-workspace orchestration SQLite",
        "seed_repo_files -- ALLOWED: bootstrap seeding runs during startup before hub handshake completes",
    ],
    "adapters/discord/cli_channels.py": [
        "open_orchestration_sqlite -- ALLOWED: CLI-only diagnostic reads hub binding rows",
    ],
    "adapters/telegram/service.py": [
        "AppServerThreadRegistry -- ALLOWED: protocol-local PMA thread ID mapping for topic routing",
    ],
    "adapters/telegram/cli_chats.py": [
        "open_orchestration_sqlite -- ALLOWED: CLI-only diagnostic reads hub binding rows",
    ],
    "adapters/telegram/handlers/commands/execution.py": [
        "AppServerThreadRegistry -- ALLOWED: protocol-local PMA thread ID mapping for topic routing",
    ],
    "adapters/telegram/handlers/commands/flows.py": [
        "build_ticket_flow_orchestration_service -- ALLOWED: ticket flow uses per-workspace orchestration SQLite",
    ],
    "adapters/agents/agent_pool_impl.py": [
        "ManagedThreadStore -- ALLOWED: agent pool manages thread execution records in hub context",
    ],
    "adapters/agents/backend_orchestrator.py": [
        "AppServerThreadRegistry -- ALLOWED: protocol-local session tracking in backend orchestrator",
    ],
}


def _is_allowlisted(file_key: str, pattern: str) -> bool:
    for entry in _SIDE_PROCESS_BOUNDARY_ALLOWLIST.get(file_key, []):
        if pattern in entry:
            return True
    return False


def _side_process_files() -> list[Path]:
    result: list[Path] = []
    for prefix in _SIDE_PROCESS_PREFIXES:
        parts = prefix.split(".")
        base = SRC_ROOT
        for part in parts[1:]:
            base = base / part
        if base.is_dir():
            for py in sorted(base.rglob("*.py")):
                if py.name == "__init__.py":
                    continue
                result.append(py)
    return result


def test_side_processes_do_not_import_hub_supervisor() -> None:
    violations: list[str] = []
    for path in _side_process_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    if "HubSupervisor" in full or alias.name == "HubSupervisor":
                        file_key = str(path.relative_to(SRC_ROOT))
                        violations.append(
                            f"{file_key}: imports HubSupervisor via 'from {module} import {alias.name}'"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "HubSupervisor" in alias.name:
                        file_key = str(path.relative_to(SRC_ROOT))
                        violations.append(
                            f"{file_key}: imports HubSupervisor via 'import {alias.name}'"
                        )
    assert (
        not violations
    ), "Side-process modules must not import HubSupervisor:\n" + "\n".join(violations)


test_side_processes_do_not_import_hub_supervisor = pytest.mark.slow(
    test_side_processes_do_not_import_hub_supervisor
)


def test_side_processes_do_not_use_notification_store_directly() -> None:
    violations: list[str] = []
    for path in _side_process_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _FORBIDDEN_NOTIFICATION_STORE_PATTERN not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == _FORBIDDEN_NOTIFICATION_STORE_PATTERN:
                        file_key = str(path.relative_to(SRC_ROOT))
                        violations.append(
                            f"{file_key}: imports PmaNotificationStore "
                            "(use hub control-plane client instead)"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _FORBIDDEN_NOTIFICATION_STORE_PATTERN in alias.name:
                        file_key = str(path.relative_to(SRC_ROOT))
                        violations.append(
                            f"{file_key}: imports PmaNotificationStore "
                            "(use hub control-plane client instead)"
                        )
    assert (
        not violations
    ), "Side-process modules must not import PmaNotificationStore:\n" + "\n".join(
        violations
    )


def test_side_processes_do_not_use_transcript_mirror_directly() -> None:
    violations: list[str] = []
    for path in _side_process_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _FORBIDDEN_TRANSCRIPT_MIRROR_PATTERN not in source:
            continue
        file_key = str(path.relative_to(SRC_ROOT))
        if _is_allowlisted(file_key, _FORBIDDEN_TRANSCRIPT_MIRROR_PATTERN):
            continue
        violations.append(
            f"{file_key}: references TranscriptMirrorStore "
            "(use hub control-plane get_transcript_history instead)"
        )
    assert (
        not violations
    ), "Side-process modules must not import TranscriptMirrorStore:\n" + "\n".join(
        violations
    )


def test_side_process_shared_state_imports_are_allowlisted() -> None:
    violations: list[str] = []
    for path in _side_process_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_key = str(path.relative_to(SRC_ROOT))
        for pattern in _FORBIDDEN_SHARED_STATE_PATTERNS:
            if pattern not in source:
                continue
            if _is_allowlisted(file_key, pattern):
                continue
            tree = ast.parse(source)
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if pattern in alias.name:
                            found = True
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if pattern in alias.name:
                            found = True
            if found:
                violations.append(
                    f"{file_key}: imports/uses {pattern} without allowlist entry"
                )
    assert not violations, (
        "Side-process shared-state imports must have an explicit allowlist entry:\n"
        + "\n".join(violations)
    )


def test_side_processes_do_not_import_polling_owners() -> None:
    violations: list[str] = []
    for path in _side_process_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tree = ast.parse(source)
        file_key = str(path.relative_to(SRC_ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    for pattern in _FORBIDDEN_POLLING_OWNER_PATTERNS:
                        if pattern in full or alias.name == pattern:
                            violations.append(
                                f"{file_key}: imports {pattern} via 'from {module} import {alias.name}'"
                            )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for pattern in _FORBIDDEN_POLLING_OWNER_PATTERNS:
                        if pattern in alias.name:
                            violations.append(
                                f"{file_key}: imports {pattern} via 'import {alias.name}'"
                            )
    assert not violations, (
        "Side-process modules must not import hub-owned polling workers/services:\n"
        + "\n".join(violations)
    )


test_side_processes_do_not_import_polling_owners = pytest.mark.slow(
    test_side_processes_do_not_import_polling_owners
)


# ---------------------------------------------------------------------------
# Config parser / destination boundary tests (TICKET-020)
# ---------------------------------------------------------------------------

_CONFIG_PARSER_MODULES = (
    "config_parsers",
    "config_types",
    "config_contract",
    "config_layering",
    "config_validation",
)


def _file_imports_module(path: Path, module_fragment: str) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    imports = extract_imports(source)
    return [
        imp
        for imp in imports
        if module_fragment in imp and imp.startswith("codex_autorunner.")
    ]


@pytest.mark.parametrize("module_name", _CONFIG_PARSER_MODULES)
def test_config_parser_modules_do_not_import_destinations(module_name: str) -> None:
    path = SRC_ROOT / "core" / f"{module_name}.py"
    if not path.exists():
        pytest.skip(f"{path} not found")
    hits = _file_imports_module(path, "destinations")
    assert (
        not hits
    ), f"{module_name}.py should not import from destinations module, found: {hits}"


def test_destinations_does_not_import_config_parsers() -> None:
    path = SRC_ROOT / "core" / "destinations.py"
    if not path.exists():
        pytest.skip(f"{path} not found")
    hits = _file_imports_module(path, "config_parsers")
    assert (
        not hits
    ), f"destinations.py should not import from config_parsers, found: {hits}"


def test_destinations_does_not_import_config_types() -> None:
    path = SRC_ROOT / "core" / "destinations.py"
    if not path.exists():
        pytest.skip(f"{path} not found")
    hits = _file_imports_module(path, "config_types")
    assert (
        not hits
    ), f"destinations.py should not import from config_types, found: {hits}"


def test_config_parsers_importable_without_destinations_in_sys_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    to_remove = [
        name
        for name in sys.modules
        if name == "codex_autorunner.core.destinations"
        or name.startswith("codex_autorunner.core.destinations.")
    ]
    saved = {name: sys.modules.pop(name) for name in to_remove}
    try:
        importlib.invalidate_caches()
        import codex_autorunner.core.config_parsers  # noqa: F401

        leaked = [
            name
            for name in sys.modules
            if name == "codex_autorunner.core.destinations"
            or name.startswith("codex_autorunner.core.destinations.")
        ]
        assert (
            not leaked
        ), f"config_parsers transitively imported destinations modules: {leaked}"
    finally:
        sys.modules.update(saved)


def test_destinations_importable_without_config_parsers_in_sys_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    to_remove = [
        name
        for name in sys.modules
        if name == "codex_autorunner.core.config_parsers"
        or name.startswith("codex_autorunner.core.config_parsers.")
    ]
    saved = {name: sys.modules.pop(name) for name in to_remove}
    try:
        importlib.invalidate_caches()
        import codex_autorunner.core.destinations  # noqa: F401

        leaked = [
            name
            for name in sys.modules
            if name == "codex_autorunner.core.config_parsers"
            or name.startswith("codex_autorunner.core.config_parsers.")
        ]
        assert (
            not leaked
        ), f"destinations transitively imported config_parsers modules: {leaked}"
    finally:
        sys.modules.update(saved)
