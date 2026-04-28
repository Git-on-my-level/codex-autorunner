from __future__ import annotations

import argparse
import json
import sys

from _autooptimize import (
    build_paths,
    build_status_payload,
    format_metric_value,
    load_iterations,
    load_run,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    paths = build_paths()
    run = load_run(paths)
    rows = load_iterations(paths)
    payload = build_status_payload(run, rows)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    metric = payload["metric"]
    baseline = payload["baseline"]
    best = payload["best"]
    print(f"AutoOptimize: {payload['goal']}")
    print(
        "Metric: "
        f"{metric.get('name')} ({metric.get('direction')}, unit={metric.get('unit') or 'n/a'})"
    )
    print(
        "Baseline: "
        f"{format_metric_value(baseline.get('value') if isinstance(baseline, dict) else None, metric.get('unit'))}"
    )
    print(
        "Best: "
        f"{format_metric_value(best.get('value') if isinstance(best, dict) else None, metric.get('unit'))}"
    )
    print(f"Iterations: {payload['iterations_count']}")
    print(f"Last decision: {payload['last_decision'] or 'n/a'}")
    guard_counts = payload["guard_status_counts"]
    if guard_counts:
        parts = [f"{name}={guard_counts[name]}" for name in sorted(guard_counts)]
        print("Guard status: " + " ".join(parts))
    absolute_delta = payload["absolute_delta"]
    percent_delta = payload["percent_delta"]
    if absolute_delta is not None:
        delta_text = f"{absolute_delta:.3f}"
        if metric.get("unit"):
            delta_text += f" {metric.get('unit')}"
        if percent_delta is not None:
            delta_text += f" ({percent_delta:.2f}%)"
        print(f"Delta from baseline: {delta_text}")
    if payload["stop_condition_hints"]:
        print("Stop-condition hints:")
        for hint in payload["stop_condition_hints"]:
            print(f"- {hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
