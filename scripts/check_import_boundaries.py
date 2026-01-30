#!/usr/bin/env python3
"""Check import boundaries between CAR layers.

Fails only on new violations compared to the allowlist.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "codex_autorunner"

LAYER_RULES = {
    "core": {
        "deny": (
            "codex_autorunner.integrations",
            "codex_autorunner.web",
            "codex_autorunner.routes",
            "codex_autorunner.cli",
            "codex_autorunner.server",
        )
    },
    "integrations": {
        "deny": (
            "codex_autorunner.web",
            "codex_autorunner.routes",
            "codex_autorunner.cli",
            "codex_autorunner.server",
        )
    },
}


@dataclass(frozen=True)
class Violation:
    importer: str
    imported: str
    line: int
    rule: str

    def key(self) -> tuple[str, str]:
        return (self.importer, self.imported)


@dataclass
class Allowlist:
    entries: dict[tuple[str, str], str]

    @classmethod
    def load(cls, path: Path) -> "Allowlist":
        if not path.exists():
            return cls(entries={})
        payload = json.loads(path.read_text())
        entries: dict[tuple[str, str], str] = {}
        for item in payload.get("violations", []):
            importer = item.get("importer")
            imported = item.get("imported")
            reason = item.get("reason", "")
            if importer and imported:
                entries[(importer, imported)] = reason
        return cls(entries=entries)


@dataclass
class ModuleContext:
    module: str
    is_init: bool

    @property
    def package(self) -> list[str]:
        parts = self.module.split(".")
        if self.is_init:
            return parts
        return parts[:-1]


def module_for_path(path: Path) -> ModuleContext | None:
    try:
        rel = path.relative_to(SRC_ROOT)
    except ValueError:
        return None
    if rel.suffix != ".py":
        return None
    parts = list(rel.parts)
    is_init = parts[-1] == "__init__.py"
    if is_init:
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    if not parts:
        return None
    return ModuleContext(module=".".join(parts), is_init=is_init)


def resolve_import_from(
    module: str | None, level: int, context: ModuleContext
) -> str | None:
    if level == 0:
        return module
    package_parts = context.package
    up = level - 1
    if up > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - up]
    if module:
        return ".".join(base_parts + module.split("."))
    return ".".join(base_parts)


def iter_imports(tree: ast.AST, context: ModuleContext) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            resolved = resolve_import_from(node.module, node.level or 0, context)
            if resolved:
                yield resolved, node.lineno


def layer_for_path(path: Path) -> str | None:
    try:
        rel = path.relative_to(PACKAGE_ROOT)
    except ValueError:
        return None
    if not rel.parts:
        return None
    top = rel.parts[0]
    if top in LAYER_RULES:
        return top
    return None


def collect_python_files(root: Path) -> Sequence[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def check_file(path: Path) -> list[Violation]:
    layer = layer_for_path(path)
    if not layer:
        return []
    rules = LAYER_RULES[layer]["deny"]
    context = module_for_path(path)
    if context is None:
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    violations: list[Violation] = []
    for imported, line in iter_imports(tree, context):
        if not imported.startswith("codex_autorunner"):
            continue
        for deny_prefix in rules:
            if imported == deny_prefix or imported.startswith(f"{deny_prefix}."):
                violations.append(
                    Violation(
                        importer=str(path.relative_to(REPO_ROOT)),
                        imported=imported,
                        line=line,
                        rule=f"{layer} -> {deny_prefix}",
                    )
                )
                break
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check import boundaries between CAR layers."
    )
    parser.add_argument(
        "--allowlist",
        default=str(REPO_ROOT / "scripts" / "import_boundaries_allowlist.json"),
        help="Path to allowlist JSON file.",
    )
    args = parser.parse_args()
    allowlist = Allowlist.load(Path(args.allowlist))

    violations: list[Violation] = []
    for path in collect_python_files(PACKAGE_ROOT):
        violations.extend(check_file(path))

    violations.sort(key=lambda v: (v.importer, v.line, v.imported))
    unallowlisted = [v for v in violations if v.key() not in allowlist.entries]
    stale = [
        key for key in allowlist.entries if key not in {v.key() for v in violations}
    ]

    if unallowlisted:
        print("New import boundary violations detected:")
        for violation in unallowlisted:
            print(
                f"- {violation.importer}:{violation.line} imports {violation.imported} "
                f"({violation.rule})"
            )
        print("\nAdd these to the allowlist (with a reason) or fix the imports.")
    if stale:
        print("\nAllowlist entries no longer needed:")
        for importer, imported in sorted(stale):
            reason = allowlist.entries.get((importer, imported), "")
            reason_suffix = f" — {reason}" if reason else ""
            print(f"- {importer} imports {imported}{reason_suffix}")

    return 1 if unallowlisted else 0


if __name__ == "__main__":
    raise SystemExit(main())
