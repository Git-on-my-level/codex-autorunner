#!/usr/bin/env python3
"""Validate high-confidence `car ...` command snippets against the live Typer/CLI tree.

Uses Click command resolution (groups vs leaf commands) so examples may include
positional args after a leaf without false positives.

Only inspects copy-paste-like contexts (inline code, embedded quotes, line-leading
commands) so English prose that happens to contain the word \"car\" is ignored.

Exits non-zero if any hint cannot resolve through the CLI tree (bad subcommand).

Usage:
    python scripts/check_cli_command_hints.py
"""

from __future__ import annotations

import ast
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import click

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
DOCS_ROOT = REPO_ROOT / "docs"

# Copy-paste shapes: backticks, embedded single/double quotes, or a full line CLI.
_INLINE_CODE = re.compile(
    r"`((?:\./)?car)(?:\s+([^`]+))?`",
)
_EMBEDDED_QUOTE = re.compile(
    r"['\"]((?:\./)?car)((?:\s+[a-zA-Z0-9_.-]+)+)['\"]",
)
_LINE_START = re.compile(
    r"(?m)^[ \t]*((?:\./)?car)(\s+.+)?\s*$",
)

# If we hit a leaf command and extra tokens look like English titles, skip (not a CLI hint).
_PROSE_TAIL = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "keys",
        "may",
        "might",
        "must",
        "need",
        "no",
        "not",
        "of",
        "on",
        "or",
        "ought",
        "present",
        "required",
        "shall",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "then",
        "there",
        "these",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yet",
        "your",
    }
)


def _load_cli() -> tuple[click.Group, frozenset[str]]:
    from typer.main import get_command

    from codex_autorunner.surfaces.cli.cli import app

    root = get_command(app)
    assert isinstance(root, click.Group)
    roots = frozenset(root.commands.keys())
    return root, roots


def _classify_hint_path(root: click.Group, path: tuple[str, ...]) -> str:
    """Return 'ok', 'bad', or 'prose' (skip reporting)."""
    if not path:
        return "bad"
    cmd: click.Command = root
    i = 0
    while i < len(path):
        if not isinstance(cmd, click.Group):
            rest = path[i:]
            if rest and rest[0].lower() in _PROSE_TAIL:
                return "prose"
            return "ok"
        name = path[i]
        if name not in cmd.commands:
            return "bad"
        cmd = cmd.commands[name]
        i += 1
    return "ok"


def _iter_py_files() -> Iterable[Path]:
    yield from SRC_ROOT.rglob("*.py")
    if TESTS_ROOT.is_dir():
        yield from TESTS_ROOT.rglob("*.py")


def _iter_md_files() -> Iterable[Path]:
    if not DOCS_ROOT.is_dir():
        return
    yield from DOCS_ROOT.rglob("*.md")


def _strings_from_ast(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            if not node.values:
                continue
            line = getattr(node, "lineno", 1)
            parts: list[str] = []
            for elt in node.values:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    parts.append(elt.value)
            if parts:
                out.append((line, "".join(parts)))
    return out


def _parse_car_suffix(fragment: str) -> tuple[str, ...] | None:
    """Parse subcommand tokens after `car `; stop before shell options (--foo, -h)."""
    rest = fragment.strip()
    parts: list[str] = []
    while rest:
        if rest[0] in "'\"`":
            break
        m = re.match(
            r"([a-zA-Z0-9][a-zA-Z0-9_.-]*)(.*)$",
            rest,
            re.DOTALL,
        )
        if not m:
            break
        raw = m.group(1)
        token = raw.rstrip(r".,;:!?)}\]'\"")
        if not token:
            break
        if token.startswith("-") and not token.startswith("--"):
            break
        parts.append(token)
        rest = m.group(2).lstrip()
        if rest.startswith("--") or (
            rest.startswith("-") and len(rest) > 1 and not rest[1].isspace()
        ):
            break
    return tuple(parts) if parts else None


def _extract_paths_from_text(
    text: str,
    *,
    root_names: frozenset[str],
) -> list[tuple[str, ...]]:
    raw_segments: list[str] = []

    for m in _INLINE_CODE.finditer(text):
        inner = (m.group(1) + (m.group(2) or "")).strip()
        raw_segments.append(inner)

    for m in _EMBEDDED_QUOTE.finditer(text):
        inner = (m.group(1) + m.group(2)).strip()
        raw_segments.append(inner)

    for m in _LINE_START.finditer(text):
        inner = (m.group(1) + (m.group(2) or "")).strip()
        raw_segments.append(inner)

    paths: list[tuple[str, ...]] = []
    for seg in raw_segments:
        if not seg.lower().startswith("car") and not seg.startswith("./car"):
            continue
        body = seg[2:] if seg.startswith("./") else seg
        if body.lower().startswith("car"):
            body = body[3:].lstrip()
        else:
            continue
        path = _parse_car_suffix(body)
        if not path:
            continue
        if path[0] not in root_names:
            continue
        paths.append(path)
    return paths


def _scan_python_files(
    root: click.Group,
    root_names: frozenset[str],
) -> list[tuple[Path, int, str, tuple[str, ...]]]:
    violations: list[tuple[Path, int, str, tuple[str, ...]]] = []
    seen: set[tuple[str, int, tuple[str, ...]]] = set()

    for path in sorted(_iter_py_files()):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        for lineno, s in _strings_from_ast(tree):
            if "car" not in s and "Car" not in s:
                continue
            for pth in _extract_paths_from_text(s, root_names=root_names):
                kind = _classify_hint_path(root, pth)
                if kind != "bad":
                    continue
                key = (str(path), lineno, pth)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    (
                        path,
                        lineno,
                        f"unknown CLI path {' '.join(pth)!r}",
                        pth,
                    )
                )
    return violations


def _scan_markdown_files(
    root: click.Group,
    root_names: frozenset[str],
) -> list[tuple[Path, int, str, tuple[str, ...]]]:
    violations: list[tuple[Path, int, str, tuple[str, ...]]] = []
    seen: set[tuple[str, int, tuple[str, ...]]] = set()

    for path in sorted(_iter_md_files()):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        for pth in _extract_paths_from_text(text, root_names=root_names):
            kind = _classify_hint_path(root, pth)
            if kind != "bad":
                continue
            needle = pth[0]
            line_no = 1
            for i, line in enumerate(lines, start=1):
                if needle in line and "car" in line:
                    line_no = i
                    break
            key = (str(path), line_no, pth)
            if key in seen:
                continue
            seen.add(key)
            violations.append((path, line_no, f"unknown CLI path {' '.join(pth)!r}", pth))
    return violations


def main() -> int:
    t0 = time.perf_counter()
    root, root_names = _load_cli()
    t1 = time.perf_counter()
    print(
        f"Loaded Click root with {len(root_names)} subcommands in {t1 - t0:.3f}s",
        file=sys.stderr,
    )

    t2 = time.perf_counter()
    py_v = _scan_python_files(root, root_names)
    t3 = time.perf_counter()
    print(f"Scanned Python sources in {t3 - t2:.3f}s", file=sys.stderr)

    t4 = time.perf_counter()
    md_v = _scan_markdown_files(root, root_names)
    t5 = time.perf_counter()
    print(f"Scanned docs in {t5 - t4:.3f}s", file=sys.stderr)

    all_v = sorted(
        set(py_v + md_v),
        key=lambda x: (str(x[0]), x[1], x[2]),
    )
    for path, ln, msg, _ in all_v:
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        print(f"{rel}:{ln}: {msg}")

    if all_v:
        print(f"\nTotal issues: {len(all_v)}", file=sys.stderr)
        return 1
    print("No invalid car command hints found.", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
