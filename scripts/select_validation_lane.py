#!/usr/bin/env python3
"""Classify changed files into a validation lane."""

from __future__ import annotations

import argparse
import importlib.util
import json
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
classify_changed_files = _VALIDATION_LANES.classify_changed_files
lane_selection_to_payload = _VALIDATION_LANES.lane_selection_to_payload
render_lane_selection = _VALIDATION_LANES.render_lane_selection


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
