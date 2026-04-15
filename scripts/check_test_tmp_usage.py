#!/usr/bin/env python3
"""Guard against non-hermetic writable /tmp usage in tests.

This check targets two classes of regressions:
1) Direct write-style operations against absolute /tmp paths.
2) Root-path keyword arguments in tests that point to /tmp and may become
   writable shared roots over time.

Known read-only or resolver-only cases can be allowlisted via JSON.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT_KEYWORD_ARGS = frozenset(
    {
        "workspace_path",
        "workspace_root",
        "repo_root",
        "run_dir",
        "scratchpad_dir",
        "final_output_path",
        "registry_root",
    }
)

TEMPFILE_DIR_CALLS = frozenset(
    {
        "tempfile.TemporaryDirectory",
        "tempfile.NamedTemporaryFile",
        "tempfile.mkdtemp",
        "TemporaryDirectory",
        "NamedTemporaryFile",
        "mkdtemp",
    }
)

PATH_WRITE_METHODS = frozenset(
    {
        "mkdir",
        "write_text",
        "write_bytes",
        "touch",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "chmod",
    }
)

MODULE_WRITE_CALLS = frozenset(
    {
        "os.mkdir",
        "os.makedirs",
        "os.remove",
        "os.unlink",
        "shutil.rmtree",
        "shutil.move",
        "shutil.copytree",
    }
)


@dataclass(frozen=True)
class Violation:
    file_path: str
    line: int
    col: int
    rule: str
    symbol: str
    snippet: str

    def key(self) -> str:
        return f"{self.file_path}:{self.line}:{self.rule}:{self.symbol}"


@dataclass
class Allowlist:
    entries: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Allowlist":
        if not path.exists():
            return cls(entries={})
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries: dict[str, str] = {}
        for item in payload.get("violations", []):
            key = item.get("key")
            reason = item.get("reason", "")
            if key:
                entries[key] = reason
        return cls(entries=entries)


def _contains_tmp(value: str) -> bool:
    return value == "/tmp" or value.startswith("/tmp/") or "/tmp/" in value


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return None


def _literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _path_literal(node: ast.expr) -> str | None:
    string_value = _literal_string(node)
    if string_value and _contains_tmp(string_value):
        return string_value

    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in {"Path", "pathlib.Path"} and node.args:
            first_arg = _literal_string(node.args[0])
            if first_arg and _contains_tmp(first_arg):
                return first_arg

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_literal(node.left)
        right = _literal_string(node.right)
        if left and right:
            return f"{left}/{right}"

    return None


def _open_mode(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = _literal_string(keyword.value)
            if mode is not None:
                return mode
    if len(call.args) >= 2:
        mode = _literal_string(call.args[1])
        if mode is not None:
            return mode
    return "r"


def _is_writable_mode(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _collect_python_files(repo_root: Path) -> Iterable[Path]:
    tests_root = repo_root / "tests"
    if not tests_root.exists():
        return []
    return sorted(path for path in tests_root.rglob("*.py") if path.is_file())


def _check_file(path: Path, *, repo_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    rel_path = str(path.relative_to(repo_root))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_name = _call_name(node.func)

        for keyword in node.keywords:
            if keyword.arg in ROOT_KEYWORD_ARGS:
                value = _path_literal(keyword.value)
                if value:
                    violations.append(
                        Violation(
                            file_path=rel_path,
                            line=keyword.value.lineno,
                            col=keyword.value.col_offset,
                            rule="tmp-root-kwarg",
                            symbol=keyword.arg,
                            snippet=value,
                        )
                    )

        if call_name in TEMPFILE_DIR_CALLS:
            for keyword in node.keywords:
                if keyword.arg == "dir":
                    value = _path_literal(keyword.value)
                    if value:
                        violations.append(
                            Violation(
                                file_path=rel_path,
                                line=keyword.value.lineno,
                                col=keyword.value.col_offset,
                                rule="tmp-tempfile-dir",
                                symbol=call_name or "call",
                                snippet=value,
                            )
                        )

        if call_name == "open" and node.args:
            target = _path_literal(node.args[0])
            if target and _is_writable_mode(_open_mode(node)):
                violations.append(
                    Violation(
                        file_path=rel_path,
                        line=node.lineno,
                        col=node.col_offset,
                        rule="tmp-direct-open-write",
                        symbol="open",
                        snippet=target,
                    )
                )

        if call_name in MODULE_WRITE_CALLS and node.args:
            target = _path_literal(node.args[0])
            if target:
                violations.append(
                    Violation(
                        file_path=rel_path,
                        line=node.lineno,
                        col=node.col_offset,
                        rule="tmp-module-write-call",
                        symbol=call_name or "call",
                        snippet=target,
                    )
                )

        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            target = _path_literal(node.func.value)
            if target and method in PATH_WRITE_METHODS:
                violations.append(
                    Violation(
                        file_path=rel_path,
                        line=node.lineno,
                        col=node.col_offset,
                        rule="tmp-path-write-method",
                        symbol=method,
                        snippet=target,
                    )
                )
            if target and method == "open" and _is_writable_mode(_open_mode(node)):
                violations.append(
                    Violation(
                        file_path=rel_path,
                        line=node.lineno,
                        col=node.col_offset,
                        rule="tmp-path-open-write",
                        symbol="Path.open",
                        snippet=target,
                    )
                )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check tests for non-hermetic writable /tmp usage and risky /tmp root kwargs."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to scan (defaults to current directory).",
    )
    parser.add_argument(
        "--allowlist",
        default="scripts/test_tmp_usage_allowlist.json",
        help="Path to allowlist JSON file.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Report all findings without applying allowlist filtering.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve(strict=False)
    allowlist = Allowlist.load((repo_root / args.allowlist).resolve(strict=False))

    violations: list[Violation] = []
    for file_path in _collect_python_files(repo_root):
        violations.extend(_check_file(file_path, repo_root=repo_root))

    violations.sort(key=lambda v: (v.file_path, v.line, v.col, v.rule))

    if args.report_only:
        if not violations:
            print("No non-hermetic /tmp test usage found.")
            return 0
        print("Detected /tmp usage requiring migration or explicit allowlist:")
        for violation in violations:
            print(
                f"  {violation.file_path}:{violation.line}:{violation.col} "
                f"{violation.rule} ({violation.symbol}) -> {violation.snippet}"
            )
        return 1

    violation_keys = {violation.key() for violation in violations}
    unallowlisted = [v for v in violations if v.key() not in allowlist.entries]
    stale_keys = sorted(key for key in allowlist.entries if key not in violation_keys)

    if unallowlisted:
        print("New non-hermetic /tmp usage detected in tests:")
        for violation in unallowlisted:
            print(
                f"  {violation.file_path}:{violation.line}:{violation.col} "
                f"{violation.rule} ({violation.symbol}) -> {violation.snippet}"
            )
            print(f"    key: {violation.key()}")
        print(
            "\nUse fixture-derived paths, or add explicit read-only/resolver entries."
        )
    if stale_keys:
        print("\nAllowlist entries no longer needed:")
        for key in stale_keys:
            reason = allowlist.entries.get(key, "")
            suffix = f" — {reason}" if reason else ""
            print(f"  {key}{suffix}")

    return 1 if unallowlisted else 0


if __name__ == "__main__":
    raise SystemExit(main())
