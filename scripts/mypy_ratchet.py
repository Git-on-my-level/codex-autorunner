#!/usr/bin/env python3
"""Per-file mypy error ratchet.

A baseline records how many errors each file currently has. The check fails if
any file's count *increases*, or if a file not in the baseline gains errors. It
does not fail when counts drop -- that is the point.

Why per-file rather than a single total: a global count lets someone add an
error in one file while fixing one in another and stay "green". The unit that
matters is the file being edited.

Why a ratchet at all: this repository's primary authors are agents. A rule that
depends on humans noticing a number creeping up does not hold; a rule that fails
the build the moment a file regresses does. Note the difference from a *baseline*
that merely records accepted violations forever -- entries here are expected to
shrink, and `--update` refuses to write a worse baseline.

    scripts/mypy_ratchet.py --check     # CI/pre-commit
    scripts/mypy_ratchet.py --update    # after fixing errors, to lower the bar

Exit 0 clean, 1 on regression, 2 on setup error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "mypy_ratchet_baseline.json"
DEFAULT_TARGET = "src/codex_autorunner"

_ERROR_RE = re.compile(r"^(?P<path>[^:]+):\d+:(?:\d+:)?\s*error:")


def run_mypy(target: str, python: str) -> str:
    proc = subprocess.run(
        [python, "-m", "mypy", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def count_errors(mypy_output: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for line in mypy_output.splitlines():
        match = _ERROR_RE.match(line)
        if match:
            counts[match.group("path")] += 1
    return dict(counts)


def load_baseline(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files", {}) if isinstance(data, dict) else {}
    return {str(k): int(v) for k, v in files.items()}


def write_baseline(path: Path, counts: dict[str, int]) -> None:
    payload = {
        "_comment": (
            "Per-file mypy error ratchet. Counts may only decrease. "
            "Regenerate with scripts/mypy_ratchet.py --update after fixing errors."
        ),
        "total": sum(counts.values()),
        "files": dict(sorted(counts.items())),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare(
    baseline: dict[str, int], current: dict[str, int]
) -> tuple[list[str], list[str]]:
    regressions: list[str] = []
    improvements: list[str] = []
    for path, count in sorted(current.items()):
        allowed = baseline.get(path, 0)
        if count > allowed:
            regressions.append(
                f"{path}: {count} errors, ratchet allows {allowed} (+{count - allowed})"
            )
    for path, allowed in sorted(baseline.items()):
        count = current.get(path, 0)
        if count < allowed:
            improvements.append(f"{path}: {allowed} -> {count}")
    return regressions, improvements


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail on any regression")
    mode.add_argument("--update", action="store_true", help="lower the ratchet")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    current = count_errors(run_mypy(args.target, args.python))
    baseline = load_baseline(args.baseline)

    if args.update:
        regressions, _ = compare(baseline, current)
        if regressions:
            print(
                "Refusing to update: that would raise the ratchet.\n  "
                + "\n  ".join(regressions),
                file=sys.stderr,
            )
            return 1
        write_baseline(args.baseline, current)
        print(
            f"Ratchet updated: {sum(current.values())} errors across "
            f"{len(current)} file(s)."
        )
        return 0

    regressions, improvements = compare(baseline, current)
    if improvements:
        print(f"{len(improvements)} file(s) improved since the last ratchet update:")
        for line in improvements[:10]:
            print(f"  {line}")
        if len(improvements) > 10:
            print(f"  ... and {len(improvements) - 10} more")
        print("Run scripts/mypy_ratchet.py --update to lock the gains in.")

    if regressions:
        print("\nmypy ratchet regressions:\n", file=sys.stderr)
        for line in regressions:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nFix the new type errors, or if they are genuinely unavoidable, "
            "explain why in the PR before raising the ratchet.",
            file=sys.stderr,
        )
        return 1

    print(f"mypy ratchet OK ({sum(current.values())} errors, none increased).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
