#!/usr/bin/env python3
"""Check that surfaces do not own flow lifecycle state.

`docs/ARCHITECTURE_BOUNDARIES.md` permits Surfaces to import from the Engine, so
a package-level import rule cannot express this constraint. What it *also* says
is that surfaces "do not become state owners; never be the only place truth
lives" -- and that is an ownership rule about call sites, not imports.

This check enforces two specific clauses of it, each scoped on principle rather
than by an allowlist:

1. No surface may call ``rotate_corrupt_flow_db``. That primitive moves a user's
   durable flow database aside. Deciding to do that is engine work; surfaces go
   through ``flows.controller_provider.recover_flow_store``, which is the single
   place that decision is made.

2. No module under ``surfaces/web/`` may construct ``FlowController``. HTTP
   request handlers must not own controller lifetimes; they resolve one through
   ``flows.controller_provider.FlowControllerProvider``, which owns construction,
   caching, and corrupt-store recovery.

Clause 2 deliberately does not extend to ``surfaces/cli/``: ``car flow worker``
is the process that *hosts* a flow run, so constructing a controller there is a
composition root, not a rendering surface reaching past its layer.

There is no allowlist. A new violation is a design decision that should be made
deliberately, by editing this file and saying why.

Exit code 0 when clean, 1 on violations, 2 on setup errors.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SURFACES_ROOT = REPO_ROOT / "src" / "codex_autorunner" / "surfaces"

# symbol -> (path prefix it is banned under, relative to surfaces/, "" = all)
BANNED_CALLS: dict[str, str] = {
    "rotate_corrupt_flow_db": "",
    "FlowController": "web",
}

REMEDIATION = {
    "rotate_corrupt_flow_db": (
        "Call flows.controller_provider.recover_flow_store(db_path, exc) instead; "
        "it owns the decision to replace a corrupt store."
    ),
    "FlowController": (
        "Resolve a controller via flows.controller_provider.FlowControllerProvider "
        "(web routes: flow_routes.definitions.controller_provider_for(state))."
    ),
}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    symbol: str

    def render(self, repo_root: Path) -> str:
        try:
            shown = self.path.relative_to(repo_root)
        except ValueError:
            shown = self.path
        return (
            f"{shown}:{self.line}: surface calls {self.symbol}(). "
            f"{REMEDIATION.get(self.symbol, '')}"
        )


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_aliases(tree: ast.Module) -> set[str]:
    """Import aliases bound in this module (``import x as y``)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    bound.add(alias.asname)
    return bound


def _shadowed_calls(tree: ast.Module) -> set[int]:
    """Line numbers of calls made through a name bound by the enclosing function.

    A function that *receives* ``archive_flow_run_artifacts`` as an injected
    dependency is following the rule, not breaking it, so calls through such a
    parameter must not be flagged.

    Scoping matters here: collecting parameter names module-wide would let one
    unrelated function's parameter suppress a genuine violation in a different
    function of the same file.
    """
    shadowed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        params = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        if args.vararg:
            params.add(args.vararg.arg)
        if args.kwarg:
            params.add(args.kwarg.arg)
        if not params:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id in params
            ):
                shadowed.add(inner.lineno)
    return shadowed


def _scope_matches(path: Path, prefix: str, root: Path) -> bool:
    if not prefix:
        return True
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return rel.parts[:1] == (prefix,)


def iter_surface_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def find_violations(root: Path = SURFACES_ROOT) -> tuple[list[Violation], list[str]]:
    """Return (violations, unreadable-file errors).

    A file this check cannot read or parse is an *error*, not a pass: silently
    skipping it is how a guardrail reports success over code it never looked at.
    """
    violations: list[Violation] = []
    errors: list[str] = []
    for path in iter_surface_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            errors.append(f"{path}: could not read or parse ({exc})")
            continue

        aliases = _module_aliases(tree)
        shadowed_lines = _shadowed_calls(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name is None or name not in BANNED_CALLS:
                continue
            if isinstance(node.func, ast.Name) and (
                node.lineno in shadowed_lines or name in aliases
            ):
                continue
            if not _scope_matches(path, BANNED_CALLS[name], root):
                continue
            violations.append(Violation(path=path, line=node.lineno, symbol=name))
    return violations, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=SURFACES_ROOT,
        help="Surfaces root to scan (default: src/codex_autorunner/surfaces)",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"error: surfaces root not found: {args.root}", file=sys.stderr)
        return 2

    violations, errors = find_violations(args.root)

    if errors:
        print("Flow lifecycle ownership could not inspect:\n", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        print(
            "\nA file this check cannot parse is not a passing file. Fix the "
            "syntax or the permissions.",
            file=sys.stderr,
        )
        return 1

    if not violations:
        print("Flow lifecycle ownership: OK (no surface owns flow lifecycle state)")
        return 0

    print("Flow lifecycle ownership violations:\n", file=sys.stderr)
    for violation in violations:
        print(f"  {violation.render(REPO_ROOT)}", file=sys.stderr)
    print(
        f"\n{len(violations)} violation(s). Surfaces render and drive state; "
        "they do not own it. See docs/ARCHITECTURE_BOUNDARIES.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
