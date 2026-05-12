#!/usr/bin/env python3
"""Fail early when staged Web source changes are missing generated assets."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
_VALIDATION_LANES_PATH = (
    REPO_ROOT / "src" / "codex_autorunner" / "core" / "validation_lanes.py"
)


def _load_validation_lanes_module():
    spec = importlib.util.spec_from_file_location(
        "_car_validation_lanes", _VALIDATION_LANES_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Unable to load validation lanes from {_VALIDATION_LANES_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_VALIDATION_LANES = _load_validation_lanes_module()
normalize_changed_path = _VALIDATION_LANES.normalize_changed_path
matches_prefix = _VALIDATION_LANES._matches_prefix

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


def needs_web_static_refresh(
    path: str, *, inspect_staged_package_json: bool = False
) -> bool:
    if path == "src/codex_autorunner/web_frontend/package.json":
        if not inspect_staged_package_json:
            return True
        return _package_json_requires_static_refresh(path)
    if path in WEB_BUILD_CONFIG_PATHS:
        return True
    if not matches_prefix(path, WEB_FRONTEND_SOURCE_PREFIX):
        return False
    return not path.endswith(WEB_SOURCE_TEST_SUFFIXES)


def _package_json_requires_static_refresh(path: str) -> bool:
    """Script-only package changes do not alter generated web_static assets."""

    try:
        before = _git_json(f"HEAD:{path}")
        after = _git_json(f":{path}")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return True
    return _without_scripts(before) != _without_scripts(after)


def _git_json(revision: str) -> dict[str, object]:
    result = subprocess.run(
        ["git", "show", revision],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    return value if isinstance(value, dict) else {}


def _without_scripts(value: dict[str, object]) -> dict[str, object]:
    copy = dict(value)
    copy.pop("scripts", None)
    return copy


def includes_web_static(path: str) -> bool:
    return matches_prefix(path, WEB_STATIC_PREFIX)


def missing_static_for_paths(
    paths: Sequence[str], *, inspect_staged_package_json: bool = False
) -> tuple[str, ...]:
    normalized_paths = tuple(
        sorted({path for path in map(normalize_changed_path, paths) if path})
    )
    if any(includes_web_static(path) for path in normalized_paths):
        return ()
    return tuple(
        path
        for path in normalized_paths
        if needs_web_static_refresh(
            path, inspect_staged_package_json=inspect_staged_package_json
        )
    )


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
    missing_paths = missing_static_for_paths(
        _read_paths(args), inspect_staged_package_json=True
    )
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
