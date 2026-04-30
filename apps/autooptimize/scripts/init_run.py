from __future__ import annotations

import argparse
import sys
import uuid

from _autooptimize import (
    atomic_write_json,
    atomic_write_jsonl,
    build_paths,
    locked_state,
    now_iso,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--direction", required=True, choices=("higher", "lower"))
    parser.add_argument("--unit")
    parser.add_argument("--target", type=float)
    parser.add_argument("--guard-command", action="append", default=[])
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--max-consecutive-discards", type=int)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    paths = build_paths()
    with locked_state(paths):
        if paths.run_path.exists() and not args.force:
            raise RuntimeError(
                f"AutoOptimize run already exists at {paths.run_path}. Re-run with --force to reset it."
            )
        created_at = now_iso()
        payload = {
            "run_id": f"autooptimize-{uuid.uuid4().hex[:12]}",
            "goal": args.goal,
            "primary_metric": {
                "name": args.metric,
                "direction": args.direction,
                "unit": args.unit,
                "target": args.target,
            },
            "guard_commands": list(args.guard_command or []),
            "stop_conditions": {
                "max_iterations": args.max_iterations,
                "max_consecutive_discards": args.max_consecutive_discards,
            },
            "baseline": None,
            "best": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        atomic_write_json(paths.run_path, payload)
        atomic_write_jsonl(paths.iterations_path, [])
        for artifact_path in (paths.summary_md_path, paths.summary_png_path):
            if artifact_path.exists():
                artifact_path.unlink()
    print(paths.run_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
