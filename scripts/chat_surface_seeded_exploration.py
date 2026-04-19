#!/usr/bin/env python3
"""Run seeded perturbation exploration for chat-surface lab scenarios."""

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

from tests.chat_surface_lab.seeded_exploration import (  # noqa: E402
    DEFAULT_EXPLORATION_ARTIFACT_DIR,
    DEFAULT_SEED_VALUES,
    available_seeded_perturbations,
    run_seeded_exploration_campaign,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run seeded exploration trials that perturb chat-surface scenario "
            "execution and preserve failing seed replay bundles."
        )
    )
    parser.add_argument(
        "--seed",
        action="append",
        type=int,
        default=None,
        help=(
            "Seed value to run (repeatable). Defaults to the built-in small seeded "
            "set."
        ),
    )
    parser.add_argument(
        "--perturbation",
        action="append",
        default=None,
        help="Perturbation ID to run (repeatable).",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_EXPLORATION_ARTIFACT_DIR,
        help=(
            "Artifact root for seeded exploration outputs "
            "(default: .codex-autorunner/diagnostics/chat-seeded-exploration/)."
        ),
    )
    parser.add_argument(
        "--list-perturbations",
        action="store_true",
        help="List supported perturbation IDs and exit.",
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

    if args.list_perturbations:
        for perturbation_id in available_seeded_perturbations():
            print(perturbation_id)
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    try:
        result = asyncio.run(
            run_seeded_exploration_campaign(
                output_dir=args.artifact_dir,
                seeds=tuple(args.seed or DEFAULT_SEED_VALUES),
                perturbation_ids=tuple(
                    args.perturbation or available_seeded_perturbations()
                ),
            )
        )
    except Exception as exc:
        logging.getLogger("chat_surface_seeded_exploration").error(
            "seeded exploration failed: %s",
            exc,
            exc_info=True,
        )
        return 1

    print(f"CHAT SURFACE SEEDED EXPLORATION run_id={result.run_id}")
    print(f"status={'PASS' if result.passed else 'FAIL'}")
    print(f"trials={result.trial_count} failures={result.failure_count}")
    print(f"summary={result.summary_path}")
    if result.failures:
        print("failure_records:")
        for failure in result.failures:
            print(f"- {failure.failure_path}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
