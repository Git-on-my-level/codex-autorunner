#!/usr/bin/env python3
"""Convert incident traces into sanitized replayable chat-surface scenarios."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from tests.chat_surface_lab.incident_replay import (  # noqa: E402
    convert_incident_trace_to_scenario,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert an incident trace JSON into a sanitized chat-surface "
            "scenario fixture that can be replayed by the lab runner."
        )
    )
    parser.add_argument(
        "--incident",
        type=Path,
        required=True,
        help="Path to the source incident JSON payload.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the generated scenario JSON file.",
    )
    parser.add_argument(
        "--scenario-id",
        default=None,
        help="Optional explicit scenario_id override for the generated fixture.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    output = convert_incident_trace_to_scenario(
        incident_path=args.incident,
        output_path=args.output,
        scenario_id=args.scenario_id,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
