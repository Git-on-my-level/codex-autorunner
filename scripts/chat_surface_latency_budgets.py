#!/usr/bin/env python3
"""Run chat-surface lab latency budgets and emit reproducible JSON artifacts."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from tests.chat_surface_lab.latency_budget_runner import (  # noqa: E402
    DEFAULT_ARTIFACT_DIR,
    available_latency_budget_scenario_ids,
    format_suite_summary,
    run_chat_surface_latency_budget_suite,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run named chat-surface UX latency budget scenarios and emit "
            "structured artifacts under .codex-autorunner/diagnostics."
        )
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenario_ids",
        default=None,
        help=(
            "Scenario ID to run (repeatable). Defaults to the committed latency "
            "suite covering required budget IDs."
        ),
    )
    parser.add_argument(
        "--profile",
        default="default",
        help="Profile label persisted in the artifact metadata.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help=(
            "Artifact root directory (default: "
            ".codex-autorunner/diagnostics/chat-latency-budgets/)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional explicit path for a copy of the suite JSON report.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List the default suite scenario IDs and exit.",
    )
    parser.add_argument(
        "--enforce-required-coverage",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Require observations for all required budget IDs. Defaults to true "
            "for the full default suite and false for explicit --scenario runs."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_scenarios:
        for scenario_id in available_latency_budget_scenario_ids():
            print(scenario_id)
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    logger = logging.getLogger("chat_surface_latency_budgets")

    try:
        result = asyncio.run(
            run_chat_surface_latency_budget_suite(
                scenario_ids=args.scenario_ids,
                artifact_dir=args.artifact_dir,
                profile=str(args.profile or "default"),
                enforce_required_coverage=args.enforce_required_coverage,
            )
        )
    except Exception as exc:
        logger.error("latency budget suite failed: %s", exc, exc_info=True)
        return 1

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json_dumps(result.payload),
            encoding="utf-8",
        )

    print(format_suite_summary(result))
    return 0 if result.passed else 1


def json_dumps(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
