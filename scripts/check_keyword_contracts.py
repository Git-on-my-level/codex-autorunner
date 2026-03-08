#!/usr/bin/env python3
"""Check for positional calls to keyword-contracted APIs.

This script detects positional argument calls to functions/methods that
require keyword arguments to prevent swapped-argument bugs where multiple
same-typed parameters are involved.

Usage:
    python scripts/check_keyword_contracts.py [--allowlist PATH]

Exit codes:
    0 - No new violations (existing violations are in allowlist)
    1 - New violations detected (not in allowlist)
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "codex_autorunner"

KEYWORD_CONTRACTED_APIS: dict[str, dict[str, str]] = {
    "is_within": {
        "module": "codex_autorunner.core.utils",
        "description": "Path containment check (root, target)",
    },
    "_is_within": {
        "module": "codex_autorunner.core.archive",
        "description": "Path containment check (root, target)",
    },
    "_path_within": {
        "module": "codex_autorunner.integrations.discord.service",
        "description": "Path containment check (root, target)",
    },
}


@dataclass(frozen=True)
class Violation:
    file_path: str
    line: int
    col: int
    function_name: str
    description: str

    def key(self) -> str:
        return f"{self.file_path}:{self.line}:{self.function_name}"


@dataclass
class Allowlist:
    entries: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Allowlist":
        if not path.exists():
            return cls(entries={})
        payload = json.loads(path.read_text())
        entries: dict[str, str] = {}
        for item in payload.get("violations", []):
            key = item.get("key")
            reason = item.get("reason", "")
            if key:
                entries[key] = reason
        return cls(entries=entries)


def collect_python_files(root: Path) -> Sequence[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def is_positional_call(node: ast.Call) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return len(node.args) > 0
    if isinstance(node.func, ast.Attribute):
        return len(node.args) > 0
    return False


def get_called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def check_file(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return violations

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return violations

    rel_path = str(path.relative_to(REPO_ROOT))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = get_called_name(node)
        if name is None:
            continue

        if name not in KEYWORD_CONTRACTED_APIS:
            continue

        if len(node.args) > 0:
            api_info = KEYWORD_CONTRACTED_APIS[name]
            violations.append(
                Violation(
                    file_path=rel_path,
                    line=node.lineno,
                    col=node.col_offset,
                    function_name=name,
                    description=api_info["description"],
                )
            )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check for positional calls to keyword-contracted APIs."
    )
    parser.add_argument(
        "--allowlist",
        default=str(REPO_ROOT / "scripts" / "keyword_contracts_allowlist.json"),
        help="Path to allowlist JSON file.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Report all violations without using allowlist (for CI).",
    )
    args = parser.parse_args()

    allowlist = Allowlist.load(Path(args.allowlist))

    violations: list[Violation] = []
    for path in collect_python_files(PACKAGE_ROOT):
        violations.extend(check_file(path))

    violations.sort(key=lambda v: (v.file_path, v.line))

    if args.report_only:
        if violations:
            print("Keyword contract violations detected:")
            for v in violations:
                print(
                    f"  {v.file_path}:{v.line}:{v.col}: "
                    f"positional call to '{v.function_name}' ({v.description})"
                )
            print(
                "\nThese APIs require keyword arguments to prevent swapped-argument bugs."
            )
            print("Update calls to use: function_name(root=..., target=...)")
            return 1
        else:
            print("No keyword contract violations found.")
            return 0

    unallowlisted = [v for v in violations if v.key() not in allowlist.entries]
    stale = [
        key for key in allowlist.entries if key not in {v.key() for v in violations}
    ]

    if unallowlisted:
        print("New keyword contract violations detected:")
        for v in unallowlisted:
            print(
                f"  {v.file_path}:{v.line}:{v.col}: "
                f"positional call to '{v.function_name}' ({v.description})"
            )
        print(
            "\nThese APIs require keyword arguments to prevent swapped-argument bugs."
        )
        print("Add to allowlist (with reason) or fix the calls.")
    if stale:
        print("\nAllowlist entries no longer needed:")
        for key in sorted(stale):
            reason = allowlist.entries.get(key, "")
            reason_suffix = f" — {reason}" if reason else ""
            print(f"  {key}{reason_suffix}")

    return 1 if unallowlisted else 0


if __name__ == "__main__":
    raise SystemExit(main())
