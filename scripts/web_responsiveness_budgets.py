#!/usr/bin/env python3
"""Run the web read-model responsiveness smoke and emit diagnostics artifacts."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from tests.chat_surface_lab.web_responsiveness_budgets import (  # noqa: E402
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_SEED,
    format_web_responsiveness_summary,
    run_web_responsiveness_budget_smoke,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed a deterministic large CAR hub, measure web responsiveness "
            "budgets, and write .codex-autorunner diagnostics artifacts."
        )
    )
    parser.add_argument(
        "--hub-root",
        type=Path,
        default=None,
        help=(
            "Optional disposable hub root to seed. Defaults to a temporary "
            "directory so live developer hub state is never used."
        ),
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help=(
            "Artifact root directory (default: "
            ".codex-autorunner/diagnostics/web-responsiveness-budgets/)."
        ),
    )
    parser.add_argument("--profile", default="default")
    parser.add_argument("--chat-count", type=int, default=DEFAULT_SEED["chat_count"])
    parser.add_argument("--repo-count", type=int, default=DEFAULT_SEED["repo_count"])
    parser.add_argument(
        "--worktree-count", type=int, default=DEFAULT_SEED["worktree_count"]
    )
    parser.add_argument(
        "--ticket-run-group-count",
        type=int,
        default=DEFAULT_SEED["ticket_run_group_count"],
    )
    parser.add_argument(
        "--timeline-event-count",
        type=int,
        default=DEFAULT_SEED["timeline_event_count"],
    )
    parser.add_argument(
        "--journal-event-count",
        type=int,
        default=DEFAULT_SEED["journal_event_count"],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional explicit path for a copy of the suite JSON report.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    seed_counts = {
        "chat_count": args.chat_count,
        "repo_count": args.repo_count,
        "worktree_count": args.worktree_count,
        "ticket_run_group_count": args.ticket_run_group_count,
        "timeline_event_count": args.timeline_event_count,
        "journal_event_count": args.journal_event_count,
    }

    if args.hub_root is not None:
        result = run_web_responsiveness_budget_smoke(
            hub_root=args.hub_root,
            artifact_dir=args.artifact_dir,
            profile=str(args.profile or "default"),
            seed_counts=seed_counts,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="car-web-responsiveness-") as tmpdir:
            result = run_web_responsiveness_budget_smoke(
                hub_root=Path(tmpdir) / "hub",
                artifact_dir=args.artifact_dir,
                profile=str(args.profile or "default"),
                seed_counts=seed_counts,
            )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result.payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(format_web_responsiveness_summary(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
