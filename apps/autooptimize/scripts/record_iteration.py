from __future__ import annotations

import argparse
import sys

from _autooptimize import (
    atomic_write_json,
    atomic_write_jsonl,
    build_paths,
    coerce_float,
    compute_best_record,
    ensure_decision,
    ensure_guard_status,
    ensure_metric_unit,
    load_iterations,
    load_run,
    locked_state,
    now_iso,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iteration", required=True, type=int)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--value", required=True, type=float)
    parser.add_argument("--unit")
    parser.add_argument("--decision", required=True)
    parser.add_argument("--commit-before")
    parser.add_argument("--commit-after")
    parser.add_argument("--guard-status", default="not_run")
    parser.add_argument("--milestone")
    parser.add_argument("--summary")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    paths = build_paths()
    with locked_state(paths):
        run = load_run(paths)
        rows = load_iterations(paths)
        duplicates = [row for row in rows if int(row.get("iteration", 0)) == args.iteration]
        if duplicates and not args.force:
            raise RuntimeError(
                f"Iteration {args.iteration} already exists. Re-run with --force to replace it."
            )

        unit = ensure_metric_unit(run, args.unit)
        timestamp = now_iso()
        record = {
            "iteration": args.iteration,
            "ticket": args.ticket,
            "hypothesis": args.hypothesis,
            "metric_value": coerce_float(args.value, label="iteration value"),
            "unit": unit,
            "decision": ensure_decision(args.decision),
            "commit_before": args.commit_before,
            "commit_after": args.commit_after,
            "guard_status": ensure_guard_status(args.guard_status),
            "milestone": args.milestone,
            "summary": args.summary,
            "created_at": timestamp,
        }

        rows = [row for row in rows if int(row.get("iteration", 0)) != args.iteration]
        rows.append(record)
        rows.sort(key=lambda item: int(item["iteration"]))
        run["best"] = compute_best_record(run, rows)
        run["updated_at"] = timestamp

        atomic_write_jsonl(paths.iterations_path, rows)
        atomic_write_json(paths.run_path, run)
    print("iteration recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
