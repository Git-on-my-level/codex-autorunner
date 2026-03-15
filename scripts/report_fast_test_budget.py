#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FastTestBudgetOffender:
    nodeid: str
    duration_seconds: float
    outcome: str


def _parse_positive_float(raw: str, *, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _parse_positive_int(raw: str, *, default: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _testcase_nodeid(testcase: ET.Element) -> str:
    file_name = str(testcase.attrib.get("file") or "").strip()
    class_name = str(testcase.attrib.get("classname") or "").strip()
    test_name = str(testcase.attrib.get("name") or "").strip() or "<unknown>"
    if file_name:
        return f"{file_name}::{test_name}"
    if class_name:
        return f"{class_name}::{test_name}"
    return test_name


def _testcase_outcome(testcase: ET.Element) -> str:
    if testcase.find("failure") is not None:
        return "failed"
    if testcase.find("error") is not None:
        return "error"
    if testcase.find("skipped") is not None:
        return "skipped"
    return "passed"


def collect_fast_test_budget_offenders(
    report_path: Path,
    *,
    threshold_seconds: float,
) -> list[FastTestBudgetOffender]:
    tree = ET.parse(report_path)
    offenders: list[FastTestBudgetOffender] = []
    for testcase in tree.iterfind(".//testcase"):
        duration_seconds = _parse_positive_float(
            str(testcase.attrib.get("time") or "0"),
            default=0.0,
        )
        if duration_seconds <= threshold_seconds:
            continue
        offenders.append(
            FastTestBudgetOffender(
                nodeid=_testcase_nodeid(testcase),
                duration_seconds=duration_seconds,
                outcome=_testcase_outcome(testcase),
            )
        )
    offenders.sort(key=lambda item: item.duration_seconds, reverse=True)
    return offenders


def build_report_lines(
    offenders: list[FastTestBudgetOffender],
    *,
    threshold_seconds: float,
    report_limit: int,
) -> list[str]:
    if not offenders:
        return []
    lines = [
        (
            "Slow fast tests: "
            f"{len(offenders)} tests exceeded the {threshold_seconds:.2f}s budget."
        ),
        (
            "Consider stubbing external/retry paths or marking intentional "
            "outliers with @pytest.mark.slow or @pytest.mark.integration."
        ),
    ]
    for offender in offenders[:report_limit]:
        lines.append(
            f"  {offender.duration_seconds:6.2f}s  [{offender.outcome}]  {offender.nodeid}"
        )
    remaining = len(offenders) - report_limit
    if remaining > 0:
        lines.append(f"  ... {remaining} more over-budget fast tests not shown")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report over-budget tests from a pytest JUnit XML report."
    )
    parser.add_argument("report_path", type=Path)
    parser.add_argument("--max-duration", default="0.5")
    parser.add_argument("--max-report", default="20")
    parser.add_argument("--fail-on-violation", action="store_true")
    args = parser.parse_args(argv)

    threshold_seconds = _parse_positive_float(args.max_duration, default=0.5)
    report_limit = _parse_positive_int(args.max_report, default=20)
    offenders = collect_fast_test_budget_offenders(
        args.report_path,
        threshold_seconds=threshold_seconds,
    )
    for line in build_report_lines(
        offenders,
        threshold_seconds=threshold_seconds,
        report_limit=report_limit,
    ):
        print(line)
    if offenders and args.fail_on_violation:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
