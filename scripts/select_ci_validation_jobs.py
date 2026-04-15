#!/usr/bin/env python3
"""Classify changed files and emit CI lane job routing flags."""

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
        description="Map changed files to CI validation lane jobs."
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
    parser.add_argument(
        "--github-output",
        help=(
            "Optional path to write GitHub Actions outputs "
            "(typically the GITHUB_OUTPUT file path)."
        ),
    )
    return parser.parse_args(argv)


def _read_paths(args: argparse.Namespace) -> tuple[str, ...]:
    if args.paths:
        return tuple(str(path) for path in args.paths)
    return tuple(line.strip() for line in sys.stdin.read().splitlines() if line.strip())


def _as_github_output_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_github_outputs(path: str, payload: dict[str, object]) -> None:
    output_keys = (
        "lane",
        "reason",
        "run_core",
        "run_web_ui",
        "run_chat_apps",
        "run_aggregate",
    )
    with Path(path).open("a", encoding="utf-8") as handle:
        for key in output_keys:
            handle.write(f"{key}={_as_github_output_value(payload[key])}\n")


def _to_ci_payload(selection_payload: dict[str, object]) -> dict[str, object]:
    payload = dict(selection_payload)
    lane = str(payload.get("lane", "aggregate"))
    payload.update(
        {
            "run_core": lane == "core",
            "run_web_ui": lane == "web-ui",
            "run_chat_apps": lane == "chat-apps",
            "run_aggregate": lane == "aggregate",
        }
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(tuple(sys.argv[1:] if argv is None else argv))
    selection = classify_changed_files(_read_paths(args))
    payload = _to_ci_payload(lane_selection_to_payload(selection))

    if args.format == "json":
        print(json.dumps(payload, sort_keys=True))
    else:
        lines = [
            render_lane_selection(selection),
            f"run_core: {_as_github_output_value(payload['run_core'])}",
            f"run_web_ui: {_as_github_output_value(payload['run_web_ui'])}",
            f"run_chat_apps: {_as_github_output_value(payload['run_chat_apps'])}",
            f"run_aggregate: {_as_github_output_value(payload['run_aggregate'])}",
        ]
        print("\n".join(lines))

    if args.github_output:
        _write_github_outputs(args.github_output, payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
