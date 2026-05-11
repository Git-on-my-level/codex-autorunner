#!/usr/bin/env python3
"""Run fast Web UI lab scenarios and emit agent-readable diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.web_ui_lab.corpus import WEB_UI_SCENARIOS
from tests.web_ui_lab.runner import DEFAULT_DIAGNOSTICS_ROOT, run_scenario
from tests.web_ui_lab.scenario_models import ScenarioTag


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run fixture-backed Web UI lab scenarios without launching a browser."
        )
    )
    parser.add_argument(
        "--diagnostics-root",
        type=Path,
        default=DEFAULT_DIAGNOSTICS_ROOT,
        help=(
            "Directory for diagnostics artifacts "
            f"(default: {DEFAULT_DIAGNOSTICS_ROOT})."
        ),
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the current latest.json summary instead of running scenarios.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    diagnostics_root = args.diagnostics_root
    latest_path = diagnostics_root / "latest.json"

    if args.summary:
        return _print_existing_summary(latest_path)

    diagnostics_root.mkdir(parents=True, exist_ok=True)
    summary = _run_fast_scenarios(diagnostics_root)
    latest_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    _print_summary(summary)
    return 0 if summary["status"] == "passed" else 1


def _run_fast_scenarios(diagnostics_root: Path) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc).isoformat()

    for scenario in WEB_UI_SCENARIOS:
        if ScenarioTag.FAST not in scenario.tags:
            continue
        try:
            report = run_scenario(scenario, diagnostics_root=diagnostics_root)
        except AssertionError as exc:
            report = getattr(exc, "report", None)
            if not isinstance(report, dict):
                report = {
                    "scenario_id": scenario.scenario_id,
                    "route_name": scenario.route_name,
                    "route_path": scenario.route_path,
                    "status": "failed",
                    "failed_invariants": [
                        {"id": "scenario_exception", "message": str(exc)}
                    ],
                }
            failures.append(_failure_summary(report, diagnostics_root))
        reports.append(_report_summary(report, diagnostics_root))

    return {
        "schema_version": 1,
        "kind": "web_ui_lab_fast_check",
        "status": "failed" if failures else "passed",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "diagnostics_root": str(diagnostics_root),
        "scenario_count": len(reports),
        "failure_count": len(failures),
        "reports": reports,
        "failures": failures,
        "inspect_first": (
            failures[0]["report_path"] if failures else str(diagnostics_root)
        ),
    }


def _report_summary(report: dict[str, Any], diagnostics_root: Path) -> dict[str, Any]:
    scenario_id = str(report["scenario_id"])
    report_path = diagnostics_root / scenario_id / "report.json"
    fixture_path = report.get(
        "fixture_payload_path",
        str(diagnostics_root / scenario_id / "fixture_payload.json"),
    )
    return {
        "scenario_id": scenario_id,
        "status": report.get("status", "failed"),
        "route_name": report.get("route_name"),
        "route_path": report.get("route_path"),
        "fixture_kind": report.get("fixture_kind"),
        "failed_invariants": report.get("failed_invariants", []),
        "report_path": str(report_path),
        "fixture_payload_path": str(fixture_path),
        "inspect_first": str(report_path),
    }


def _failure_summary(report: dict[str, Any], diagnostics_root: Path) -> dict[str, Any]:
    scenario_id = str(report.get("scenario_id"))
    report_path = diagnostics_root / scenario_id / "report.json"
    return {
        "scenario_id": report.get("scenario_id"),
        "route_name": report.get("route_name"),
        "route_path": report.get("route_path"),
        "failed_invariants": report.get("failed_invariants", []),
        "report_path": str(report_path),
        "inspect_first": str(report_path),
    }


def _print_existing_summary(latest_path: Path) -> int:
    if not latest_path.exists():
        print(f"No Web UI lab summary found at {latest_path}", file=sys.stderr)
        return 2
    try:
        summary = json.loads(latest_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Invalid Web UI lab summary at {latest_path}: {exc}", file=sys.stderr)
        return 2
    _print_summary(summary)
    return 0 if summary.get("status") == "passed" else 1


def _print_summary(summary: dict[str, Any]) -> None:
    status = str(summary.get("status", "unknown")).upper()
    scenario_count = summary.get("scenario_count", 0)
    failure_count = summary.get("failure_count", 0)
    print(
        f"Web UI lab fast check: {status} "
        f"({scenario_count} scenario(s), {failure_count} failure(s))"
    )
    failures = summary.get("failures", [])
    if failures:
        print("Failures:")
        for failure in failures:
            invariants = failure.get("failed_invariants") or []
            invariant_ids = ", ".join(
                str(item.get("id", "unknown")) for item in invariants
            )
            print(
                "  "
                f"{failure.get('scenario_id')} "
                f"{failure.get('route_path')} "
                f"[{invariant_ids or 'unknown'}] "
                f"inspect={failure.get('inspect_first')}"
            )
    else:
        print(f"Diagnostics: {summary.get('inspect_first')}")


if __name__ == "__main__":
    raise SystemExit(main())
