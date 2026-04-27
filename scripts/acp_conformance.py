#!/usr/bin/env python3
"""Run ACP conformance checks against fixture and configured repo targets."""

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

from tests.chat_surface_lab.acp_conformance import (  # noqa: E402
    DEFAULT_ARTIFACT_DIR,
    discover_repo_acp_targets,
    format_acp_conformance_report,
    run_acp_conformance_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run ACP conformance checks against the deterministic fixture and "
            "configured ACP-backed runtimes in this repo."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repo root used for config discovery.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help="Directory used for latest.json and per-run suite_report.json artifacts.",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="target_ids",
        default=None,
        help="Optional target id filter. Repeat to run multiple targets.",
    )
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="List discovered target ids and exit.",
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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    repo_root = Path(args.repo_root).resolve()
    discovered = discover_repo_acp_targets(repo_root)
    if args.list_targets:
        for target in discovered:
            print(f"{target.id}\t{target.label}\t{' '.join(target.command)}")
        return 0

    target_ids = {
        str(value).strip() for value in (args.target_ids or []) if str(value).strip()
    }
    selected = [
        target for target in discovered if not target_ids or target.id in target_ids
    ]
    if target_ids and not selected:
        parser.error("No discovered targets matched --target filters.")

    report = asyncio.run(
        run_acp_conformance_report(
            repo_root,
            targets=selected,
            artifact_dir=Path(args.artifact_dir),
        )
    )
    print(format_acp_conformance_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
