#!/usr/bin/env python3
"""Classify changed files into a validation lane."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from codex_autorunner.core.validation_lanes import (  # noqa: E402
    classify_changed_files,
    lane_selection_to_payload,
    render_lane_selection,
)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Map changed files to one validation lane: "
            "core, web-ui, chat-apps, or aggregate."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Changed file paths. If omitted, paths are read from stdin as "
            "newline-delimited entries."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser.parse_args(argv)


def _read_paths(args: argparse.Namespace) -> tuple[str, ...]:
    if args.paths:
        return tuple(str(path) for path in args.paths)
    return tuple(line.strip() for line in sys.stdin.read().splitlines() if line.strip())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(tuple(sys.argv[1:] if argv is None else argv))
    selection = classify_changed_files(_read_paths(args))

    if args.format == "json":
        payload = lane_selection_to_payload(selection)
        print(json.dumps(payload, sort_keys=True))
        return 0

    print(render_lane_selection(selection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
