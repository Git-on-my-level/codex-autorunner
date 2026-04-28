from __future__ import annotations

import argparse
import sys

from _autooptimize import (
    atomic_write_json,
    build_paths,
    coerce_float,
    ensure_metric_unit,
    load_run,
    locked_state,
    now_iso,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--value", required=True, type=float)
    parser.add_argument("--unit")
    parser.add_argument("--summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    paths = build_paths()
    with locked_state(paths):
        run = load_run(paths)
        unit = ensure_metric_unit(run, args.unit)
        timestamp = now_iso()
        value = coerce_float(args.value, label="baseline value")
        baseline = {
            "value": value,
            "unit": unit,
            "summary": args.summary,
            "created_at": timestamp,
        }
        run["baseline"] = baseline
        run["best"] = {
            "value": value,
            "unit": unit,
            "summary": args.summary,
            "source": "baseline",
            "iteration": None,
            "ticket": None,
            "created_at": timestamp,
        }
        run["updated_at"] = timestamp
        atomic_write_json(paths.run_path, run)
    print("baseline recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
