#!/usr/bin/env python3
"""Compare svelte-check warnings against the committed warning baseline."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "svelte_warning_baseline.json"
LOCATION_RE = re.compile(r"^(?P<path>.+\.svelte):\d+:\d+$")


@dataclass(frozen=True, order=True)
class SvelteWarning:
    path: str
    message: str


def parse_svelte_warnings(
    output: str, *, repo_root: Path = REPO_ROOT
) -> tuple[SvelteWarning, ...]:
    warnings: list[SvelteWarning] = []
    current_path: str | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        location_match = LOCATION_RE.match(line)
        if location_match:
            current_path = _repo_relative_path(
                Path(location_match.group("path")), repo_root
            )
            continue
        if current_path and line.startswith("Warn: "):
            warnings.append(
                SvelteWarning(path=current_path, message=line.removeprefix("Warn: "))
            )
            current_path = None

    return tuple(sorted(warnings))


def load_warning_baseline(path: Path) -> tuple[SvelteWarning, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        sorted(
            SvelteWarning(path=str(item["path"]), message=str(item["message"]))
            for item in payload.get("warnings", [])
        )
    )


def _repo_relative_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when svelte-check emits warnings outside the baseline."
    )
    parser.add_argument("output", help="Path to captured svelte-check output.")
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help="Path to the JSON warning baseline.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(tuple(sys.argv[1:] if argv is None else argv))
    current = set(parse_svelte_warnings(Path(args.output).read_text(encoding="utf-8")))
    baseline = set(load_warning_baseline(Path(args.baseline)))

    new_warnings = sorted(current - baseline)
    stale_warnings = sorted(baseline - current)

    if new_warnings:
        print(
            "New Svelte warnings found outside scripts/svelte_warning_baseline.json:",
            file=sys.stderr,
        )
        for warning in new_warnings:
            print(f"  - {warning.path}: {warning.message}", file=sys.stderr)
        return 1

    if stale_warnings:
        print(
            "Svelte warning baseline has stale entries; consider removing:",
            file=sys.stderr,
        )
        for warning in stale_warnings:
            print(f"  - {warning.path}: {warning.message}", file=sys.stderr)

    print(f"Svelte warning baseline matched ({len(current)} warning(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
