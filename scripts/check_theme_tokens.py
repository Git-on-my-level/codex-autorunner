#!/usr/bin/env python3
"""Flag hardcoded colors in the Web Hub frontend.

Theme tokens live in ``src/app.css`` (``:root`` + ``[data-theme="dark"]``) and
``src/theme-presets.css``. Every other component must consume those tokens via
``var(--color-*)`` / ``var(--shadow-*)`` so themes stay swappable.

This check scans ``*.css`` and ``<style>`` blocks in ``*.svelte`` for color
literals (``#hex``, ``rgb()``, ``rgba()``, ``hsl()``, ``hsla()``) that appear
in declared property values. Lines tagged with ``/* theme-allow */`` are
skipped — use sparingly for genuine exceptions (e.g. opacity-only mixes that
don't map to a token).

A baseline of historically-known violations lives at
``scripts/theme_tokens_baseline.json`` so the check only fails on *new*
hardcoded colors. Refresh after intentionally adding/removing an entry:

  python scripts/check_theme_tokens.py --update-baseline

Exit code 0 on success, 1 if new hardcoded colors are found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = REPO_ROOT / "src/codex_autorunner/web_frontend/src"
BASELINE_PATH = REPO_ROOT / "scripts/theme_tokens_baseline.json"

# Files where token definitions are allowed.
TOKEN_FILES = {
    FRONTEND_SRC / "theme-presets.css",
}

APP_CSS = FRONTEND_SRC / "app.css"

COLOR_RE = re.compile(
    r"(#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b"
    r"|rgba?\([^)]*\)"
    r"|hsla?\([^)]*\))"
)
ALLOW_MARKER = "theme-allow"
STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL)
TOKEN_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*(?::root|\[data-theme=\"[^\"]+\"\])\s*\{[^{}]*\}",
)


def _redact(text: str, start: int, end: int) -> str:
    chunk = text[start:end]
    return text[:start] + "".join("\n" if c == "\n" else " " for c in chunk) + text[end:]


def _scannable_text(path: Path, raw: str) -> str:
    if path.suffix == ".svelte":
        kept = ["\n" if c == "\n" else " " for c in raw]
        for match in STYLE_BLOCK_RE.finditer(raw):
            body_start, body_end = match.start(1), match.end(1)
            kept[body_start:body_end] = list(raw[body_start:body_end])
        return "".join(kept)

    if path == APP_CSS:
        text = raw
        while True:
            match = TOKEN_BLOCK_RE.search(text)
            if not match:
                break
            text = _redact(text, match.start(), match.end())
        return text

    return raw


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for ext in ("*.css", "*.svelte"):
        files.extend(sorted(FRONTEND_SRC.rglob(ext)))
    return [p for p in files if p not in TOKEN_FILES]


def _scan(path: Path) -> list[tuple[int, str, str]]:
    raw = path.read_text(encoding="utf-8")
    scannable = _scannable_text(path, raw)
    raw_lines = raw.splitlines()
    scan_lines = scannable.splitlines()
    hits: list[tuple[int, str, str]] = []
    for lineno, scan_line in enumerate(scan_lines, start=1):
        if not scan_line.strip():
            continue
        if ALLOW_MARKER in scan_line:
            continue
        stripped = scan_line.strip()
        if stripped.startswith("/*") or stripped.startswith("*"):
            continue
        for color_match in COLOR_RE.finditer(scan_line):
            literal = color_match.group(0)
            hits.append((lineno, raw_lines[lineno - 1].rstrip(), literal))
    return hits


def _collect_violations() -> dict[str, list[str]]:
    """Map repo-relative file path -> sorted list of color literals found."""
    result: dict[str, list[str]] = {}
    for path in _iter_files():
        literals = sorted(literal for _, _, literal in _scan(path))
        if literals:
            rel = str(path.relative_to(REPO_ROOT))
            result[rel] = literals
    return result


def _load_baseline() -> dict[str, list[str]]:
    if not BASELINE_PATH.is_file():
        return {}
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Malformed baseline at {BASELINE_PATH}")
    return {str(k): sorted(map(str, v)) for k, v in data.items()}


def _save_baseline(violations: dict[str, list[str]]) -> None:
    payload = {k: violations[k] for k in sorted(violations)}
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _diff(current: dict[str, list[str]], baseline: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return literals present in current but missing from baseline counts."""
    new: dict[str, list[str]] = {}
    for path, literals in current.items():
        allowed = list(baseline.get(path, []))
        extras: list[str] = []
        for literal in literals:
            if literal in allowed:
                allowed.remove(literal)  # consume one allowance
            else:
                extras.append(literal)
        if extras:
            new[path] = extras
    return new


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite the baseline file with the current set of violations.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    if not FRONTEND_SRC.is_dir():
        print(f"Frontend source dir not found: {FRONTEND_SRC}", file=sys.stderr)
        return 2

    current = _collect_violations()

    if args.update_baseline:
        _save_baseline(current)
        total = sum(len(v) for v in current.values())
        print(f"Wrote baseline with {total} entries across {len(current)} files.")
        return 0

    baseline = _load_baseline()
    new = _diff(current, baseline)

    if new:
        print("New hardcoded color literals detected — use theme tokens instead.\n")
        for path in sorted(new):
            for literal in new[path]:
                print(f"  {path}: {literal}")
        print(
            "\nDefine new tokens in src/app.css (:root + [data-theme=\"dark\"]) and "
            "src/theme-presets.css, then reference them with var(--…).\n"
            "Append /* theme-allow */ on a line if no token applies, or run\n"
            "  python scripts/check_theme_tokens.py --update-baseline\n"
            "if you intentionally added the literal."
        )
        return 1

    total = sum(len(v) for v in current.values())
    print(
        f"Theme token check OK — scanned {len(_iter_files())} files, "
        f"{total} baselined literal(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
