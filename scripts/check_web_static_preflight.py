#!/usr/bin/env python3
"""Fail early when staged Web source changes are missing generated assets."""

from __future__ import annotations

import argparse
import sys
from pathlib import PurePosixPath
from typing import Sequence

WEB_FRONTEND_SOURCE_PREFIX = "src/codex_autorunner/web_frontend/src"
WEB_STATIC_PREFIX = "src/codex_autorunner/web_static"
WEB_BUILD_CONFIG_PATHS = frozenset(
    {
        "src/codex_autorunner/web_frontend/package.json",
        "src/codex_autorunner/web_frontend/svelte.config.js",
        "src/codex_autorunner/web_frontend/vite.config.ts",
    }
)
WEB_SOURCE_TEST_SUFFIXES = (
    ".test.js",
    ".test.ts",
    ".test.svelte",
    ".spec.js",
    ".spec.ts",
    ".spec.svelte",
)


def normalize_path(raw_path: str) -> str:
    path = str(raw_path).strip().replace("\\", "/")
    if not path:
        return ""
    normalized = PurePosixPath(path).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return "" if normalized == "." else normalized


def needs_web_static_refresh(path: str) -> bool:
    if path in WEB_BUILD_CONFIG_PATHS:
        return True
    if not _matches_prefix(path, WEB_FRONTEND_SOURCE_PREFIX):
        return False
    return not path.endswith(WEB_SOURCE_TEST_SUFFIXES)


def includes_web_static(path: str) -> bool:
    return _matches_prefix(path, WEB_STATIC_PREFIX)


def missing_static_for_paths(paths: Sequence[str]) -> tuple[str, ...]:
    normalized_paths = tuple(
        sorted({path for path in map(normalize_path, paths) if path})
    )
    if any(includes_web_static(path) for path in normalized_paths):
        return ()
    return tuple(path for path in normalized_paths if needs_web_static_refresh(path))


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check staged Web source paths for missing committed web_static output."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Changed paths. If omitted, newline-delimited paths are read from stdin.",
    )
    return parser.parse_args(argv)


def _read_paths(args: argparse.Namespace) -> tuple[str, ...]:
    if args.paths:
        return tuple(args.paths)
    return tuple(line.strip() for line in sys.stdin.read().splitlines() if line.strip())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(tuple(sys.argv[1:] if argv is None else argv))
    missing_paths = missing_static_for_paths(_read_paths(args))
    if not missing_paths:
        return 0

    print(
        "Web frontend source changes are staged without generated web_static output.",
        file=sys.stderr,
    )
    print(
        "Run 'pnpm run build' and stage src/codex_autorunner/web_static/.",
        file=sys.stderr,
    )
    print("Source paths requiring generated assets:", file=sys.stderr)
    for path in missing_paths:
        print(f"  - {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
