#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from codex_autorunner.core.hermes_readiness import (  # noqa: E402
    collect_signal_results,
    compute_scorecard,
    default_signal_specs,
    load_signal_results,
    render_scorecard_text,
)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute Hermes readiness scorecard from objective checks."
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root path (default: current repo root)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run pytest checks",
    )
    parser.add_argument(
        "--ci-smoke",
        action="store_true",
        help="Run CI-smoke signal set",
    )
    parser.add_argument(
        "--signals-json",
        default="",
        help="Path to precomputed signal JSON payload (skips live checks)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    if args.signals_json:
        signals = load_signal_results(Path(args.signals_json))
    else:
        specs = default_signal_specs(ci_smoke=args.ci_smoke)
        signals = collect_signal_results(
            specs,
            repo_root=repo_root,
            python_executable=args.python,
        )

    scorecard = compute_scorecard(signals)
    payload = {
        "scorecard": scorecard.to_dict(),
        "signals": [signal.to_dict() for signal in signals],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_scorecard_text(scorecard, signals=signals))

    return 0 if scorecard.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
