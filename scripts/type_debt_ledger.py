#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from codex_autorunner.core.type_debt_ledger import (
    build_type_debt_ledger,
    ledger_to_dict,
    render_markdown_report,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a typed debt ledger from Dict[str, Any] / dict[str, Any] hotspots."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT / "src" / "codex_autorunner",
        help="Source root to scan. Defaults to src/codex_autorunner.",
    )
    parser.add_argument(
        "--max-hotspots-per-slice",
        type=int,
        default=45,
        help="Upper bound for hotspots grouped into each recommended slice.",
    )
    parser.add_argument(
        "--max-modules-per-slice",
        type=int,
        default=4,
        help="Upper bound for modules grouped into each recommended slice.",
    )
    parser.add_argument(
        "--top-modules",
        type=int,
        default=20,
        help="How many modules to show in markdown output.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format.",
    )
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="Optional output file path.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    ledger = build_type_debt_ledger(
        args.root,
        max_hotspots_per_slice=args.max_hotspots_per_slice,
        max_modules_per_slice=args.max_modules_per_slice,
    )
    if args.format == "json":
        rendered = json.dumps(ledger_to_dict(ledger), indent=2, sort_keys=True) + "\n"
    else:
        rendered = render_markdown_report(ledger, top_modules=args.top_modules)
    if args.write is not None:
        args.write.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
