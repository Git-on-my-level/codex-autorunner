from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import Any

from _autooptimize import (
    build_paths,
    build_status_payload,
    coerce_float,
    compare_is_better,
    load_iterations,
    load_run,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _discard_tail(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in reversed(rows):
        if row.get("decision") == "discard":
            count += 1
            continue
        break
    return count


def _target_reached(run: dict[str, Any]) -> bool:
    metric = run.get("primary_metric") or {}
    target = metric.get("target")
    best = run.get("best")
    if target is None or not isinstance(best, dict):
        return False
    direction = str(metric.get("direction") or "")
    best_value = coerce_float(best.get("value"), label="best.value")
    target_value = coerce_float(target, label="target")
    return best_value == target_value or compare_is_better(
        best_value,
        target_value,
        direction,
    )


def build_plan(run: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    status = build_status_payload(run, rows)
    goal = str(run.get("goal") or "").strip()
    stop_conditions = run.get("stop_conditions") or {}
    max_iterations = stop_conditions.get("max_iterations")
    max_discards = stop_conditions.get("max_consecutive_discards")

    reasons: list[str] = []
    template = "iteration"
    next_iteration = len(rows) + 1

    if not isinstance(run.get("baseline"), dict):
        template = "baseline"
        next_iteration = None
        reasons.append("baseline not recorded")
    elif _target_reached(run):
        template = "closeout"
        next_iteration = None
        reasons.append("metric target reached")
    elif max_iterations is not None and len(rows) >= int(max_iterations):
        template = "closeout"
        next_iteration = None
        reasons.append("max_iterations reached")
    elif max_discards is not None and _discard_tail(rows) >= int(max_discards):
        template = "closeout"
        next_iteration = None
        reasons.append("max_consecutive_discards reached")
    else:
        reasons.append("next measurable hypothesis required")

    command = [
        "car",
        "apps",
        "apply",
        "blessed.autooptimize",
        "--template",
        template,
    ]
    if goal:
        command.extend(["--set", f"goal={goal}"])

    return {
        "template": template,
        "next_iteration": next_iteration,
        "reason": "; ".join(reasons),
        "command": " ".join(shlex.quote(part) for part in command),
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    paths = build_paths()
    run = load_run(paths)
    rows = load_iterations(paths)
    plan = build_plan(run, rows)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    print(f"Next AutoOptimize template: {plan['template']}")
    if plan["next_iteration"] is not None:
        print(f"Next iteration: {plan['next_iteration']}")
    print(f"Reason: {plan['reason']}")
    print(f"Apply command: {plan['command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
