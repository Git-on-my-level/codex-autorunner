#!/usr/bin/env python3
"""Estimate how much CAR production code is state-machine-owned.

This is intentionally a heuristic metric for AutoOptimize campaigns. It counts
non-empty, non-comment Python lines under ``src/codex_autorunner`` and classifies
lines as state-machine-owned when they live in known state-machine modules or in
top-level functions/classes with state-machine naming signals.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_SOURCE_ROOT = Path("src/codex_autorunner")
EXCLUDED_PARTS = {
    "__pycache__",
    "web_static",
    "static",
}

STATE_MACHINE_PATH_PARTS = {
    "flows",
    "pma_domain",
    "orchestration",
    "tickets",
}

STATE_MACHINE_PATH_PREFIXES = (
    ("adapters", "app_server"),
    ("agents", "acp"),
    ("adapters", "chat"),
    ("core", "hub_control_plane"),
    ("core", "managed_processes"),
    ("core", "pma"),
)

STATE_MACHINE_FILENAME_MARKERS = (
    "action",
    "approval",
    "controller",
    "dispatch",
    "execution",
    "flow",
    "lifecycle",
    "managed_thread",
    "pma",
    "policy",
    "queue",
    "reducer",
    "runtime",
    "state",
    "status",
    "supervisor",
    "transition",
)

STATE_MACHINE_SYMBOL_MARKERS = (
    "action",
    "approval",
    "controller",
    "dispatch",
    "event",
    "execute",
    "flow",
    "lifecycle",
    "machine",
    "policy",
    "queue",
    "reduce",
    "reducer",
    "runtime",
    "state",
    "status",
    "step",
    "supervisor",
    "transition",
    "turn",
)


@dataclass(frozen=True)
class FileCoverage:
    path: str
    total_loc: int
    state_machine_loc: int
    reason: str

    @property
    def percent(self) -> float:
        if self.total_loc == 0:
            return 0.0
        return self.state_machine_loc / self.total_loc * 100.0


def is_code_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def code_lines_by_number(source: str) -> dict[int, str]:
    return {
        line_number: line
        for line_number, line in enumerate(source.splitlines(), start=1)
        if is_code_line(line)
    }


def should_exclude(path: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.parts)


def classify_path(relative_path: Path) -> str | None:
    for prefix in STATE_MACHINE_PATH_PREFIXES:
        if relative_path.parts[: len(prefix)] == prefix:
            return "state-machine path prefix: " + "/".join(prefix)

    parts = set(relative_path.parts)
    if parts & STATE_MACHINE_PATH_PARTS:
        return "state-machine path"

    stem = relative_path.stem.lower()
    for marker in STATE_MACHINE_FILENAME_MARKERS:
        if marker in stem:
            return f"filename marker: {marker}"
    return None


def symbol_is_state_machine(name: str) -> str | None:
    lower = name.lower()
    for marker in STATE_MACHINE_SYMBOL_MARKERS:
        if marker in lower:
            return f"symbol marker: {marker}"
    return None


def node_line_numbers(node: ast.AST, code_lines: dict[int, str]) -> set[int]:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None or end is None:
        return set()
    return {
        line_number
        for line_number in range(start, end + 1)
        if line_number in code_lines
    }


def classify_file(path: Path, root: Path) -> FileCoverage:
    relative_path = path.relative_to(root)
    source = path.read_text(encoding="utf-8")
    code_lines = code_lines_by_number(source)
    total_loc = len(code_lines)
    path_reason = classify_path(relative_path)
    if path_reason:
        return FileCoverage(str(relative_path), total_loc, total_loc, path_reason)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return FileCoverage(str(relative_path), total_loc, 0, "syntax error")

    state_lines: set[int] = set()
    reasons: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            reason = symbol_is_state_machine(node.name)
            if reason:
                state_lines.update(node_line_numbers(node, code_lines))
                reasons.add(reason)

    return FileCoverage(
        str(relative_path),
        total_loc,
        len(state_lines),
        ", ".join(sorted(reasons)) if reasons else "none",
    )


def iter_python_files(source_root: Path) -> Iterable[Path]:
    for path in sorted(source_root.rglob("*.py")):
        if not should_exclude(path.relative_to(source_root)):
            yield path


def estimate(source_root: Path) -> dict[str, object]:
    files = [
        classify_file(path, source_root) for path in iter_python_files(source_root)
    ]
    total_loc = sum(file.total_loc for file in files)
    state_machine_loc = sum(file.state_machine_loc for file in files)
    percent = (state_machine_loc / total_loc * 100.0) if total_loc else 0.0

    by_path = sorted(
        files,
        key=lambda file: (file.state_machine_loc - file.total_loc, file.total_loc),
    )
    biggest_non_state = [
        {
            "path": file.path,
            "total_loc": file.total_loc,
            "state_machine_loc": file.state_machine_loc,
            "reason": file.reason,
        }
        for file in by_path
        if file.total_loc > file.state_machine_loc
    ]

    return {
        "metric": "state_machine_coverage_percent",
        "direction": "higher",
        "unit": "percent",
        "source_root": str(source_root),
        "files": len(files),
        "total_loc": total_loc,
        "state_machine_loc": state_machine_loc,
        "non_state_machine_loc": total_loc - state_machine_loc,
        "coverage_percent": round(percent, 4),
        "biggest_non_state_files": biggest_non_state,
    }


def format_text(result: dict[str, object], *, top: int) -> str:
    lines = [
        f"Metric: {result['metric']}",
        f"Coverage: {result['coverage_percent']} {result['unit']}",
        f"State-machine LOC: {result['state_machine_loc']} / {result['total_loc']}",
        f"Files scanned: {result['files']}",
        "",
        f"Top {top} non-state-machine files by remaining LOC:",
    ]
    files = result["biggest_non_state_files"]
    assert isinstance(files, list)
    for file in files[:top]:
        assert isinstance(file, dict)
        remaining = int(file["total_loc"]) - int(file["state_machine_loc"])
        lines.append(f"- {file['path']}: {remaining} LOC ({file['reason']})")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help="Python source root to scan.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--top", type=int, default=15, help="Text report item count.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = args.source_root.resolve()
    if not source_root.exists():
        raise SystemExit(f"source root not found: {source_root}")
    result = estimate(source_root)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(format_text(result, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
