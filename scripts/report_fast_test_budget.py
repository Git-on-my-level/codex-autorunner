#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class FastTestBudgetOffender:
    nodeid: str
    duration_seconds: float
    outcome: str


DurationCollector = Callable[[list[str], Path], dict[str, float]]


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
    return testcase.attrib.get("_normalized_nodeid") or _raw_testcase_nodeid(testcase)


def _raw_testcase_nodeid(testcase: ET.Element) -> str:
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


def _load_selected_nodeids(path: Optional[Path]) -> Optional[set[str]]:
    if path is None:
        return None
    selected: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or "tests collected" in line:
            continue
        selected.add(line)
    return selected


def _resolve_classname_parts(
    repo_root: Path, class_name: str
) -> tuple[Optional[str], tuple[str, ...]]:
    if not class_name:
        return None, ()
    parts = tuple(part for part in class_name.split(".") if part)
    for index in range(len(parts), 0, -1):
        candidate = repo_root.joinpath(*parts[:index]).with_suffix(".py")
        if candidate.exists():
            rel_path = candidate.relative_to(repo_root).as_posix()
            return rel_path, parts[index:]
    return None, ()


def normalize_testcase_nodeid(testcase: ET.Element, *, repo_root: Path) -> str:
    test_name = str(testcase.attrib.get("name") or "").strip() or "<unknown>"
    file_name = str(testcase.attrib.get("file") or "").strip()
    class_name = str(testcase.attrib.get("classname") or "").strip()

    file_tail: tuple[str, ...] = ()
    if class_name:
        _resolved_file, resolved_tail = _resolve_classname_parts(repo_root, class_name)
        file_tail = resolved_tail

    if file_name:
        suffix = f"::{'::'.join(file_tail)}" if file_tail else ""
        return f"{file_name}{suffix}::{test_name}"

    resolved_file, resolved_tail = _resolve_classname_parts(repo_root, class_name)
    if resolved_file:
        suffix = f"::{'::'.join(resolved_tail)}" if resolved_tail else ""
        return f"{resolved_file}{suffix}::{test_name}"

    return _raw_testcase_nodeid(testcase)


def _percentile(sorted_durations: list[float], percentile: float) -> float:
    if not sorted_durations:
        return 0.0
    if len(sorted_durations) == 1:
        return sorted_durations[0]
    index = (len(sorted_durations) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(sorted_durations) - 1)
    frac = index - lower
    return sorted_durations[lower] * (1.0 - frac) + sorted_durations[upper] * frac


def summarize_durations(durations: Iterable[float]) -> dict[str, float | int]:
    values = sorted(durations)
    if not values:
        return {"count": 0, "p80": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "p80": _percentile(values, 0.8),
        "p90": _percentile(values, 0.9),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": values[-1],
    }


def collect_fast_test_budget_offenders(
    report_path: Path,
    *,
    threshold_seconds: float,
    selected_nodeids: Optional[set[str]] = None,
    repo_root: Optional[Path] = None,
) -> list[FastTestBudgetOffender]:
    tree = ET.parse(report_path)
    resolved_repo_root = (repo_root or Path.cwd()).resolve()
    offenders: list[FastTestBudgetOffender] = []
    for testcase in tree.iterfind(".//testcase"):
        outcome = _testcase_outcome(testcase)
        if outcome == "skipped":
            continue
        normalized_nodeid = normalize_testcase_nodeid(
            testcase, repo_root=resolved_repo_root
        )
        testcase.attrib["_normalized_nodeid"] = normalized_nodeid
        if selected_nodeids is not None and normalized_nodeid not in selected_nodeids:
            continue
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
                outcome=outcome,
            )
        )
    offenders.sort(key=lambda item: item.duration_seconds, reverse=True)
    return offenders


def build_report_lines(
    offenders: list[FastTestBudgetOffender],
    *,
    threshold_seconds: float,
    report_limit: int,
    summary: Optional[dict[str, float | int]] = None,
) -> list[str]:
    if not offenders and summary is None:
        return []
    lines: list[str] = []
    if summary is not None:
        lines.append(
            "Fast test duration summary: "
            f"count={summary['count']} "
            f"p80={float(summary['p80']):.2f}s "
            f"p90={float(summary['p90']):.2f}s "
            f"p95={float(summary['p95']):.2f}s "
            f"p99={float(summary['p99']):.2f}s "
            f"max={float(summary['max']):.2f}s"
        )
    if not offenders:
        return lines
    lines.extend(
        [
            (
                "Slow fast tests: "
                f"{len(offenders)} tests exceeded the {threshold_seconds:.2f}s budget."
            ),
            (
                "Consider stubbing external/retry paths or marking intentional "
                "outliers with @pytest.mark.slow or @pytest.mark.integration."
            ),
        ]
    )
    for offender in offenders[:report_limit]:
        lines.append(
            f"  {offender.duration_seconds:6.2f}s  [{offender.outcome}]  {offender.nodeid}"
        )
    remaining = len(offenders) - report_limit
    if remaining > 0:
        lines.append(f"  ... {remaining} more over-budget fast tests not shown")
    return lines


def _collect_call_durations_from_pytest(
    nodeids: list[str],
    repo_root: Path,
) -> dict[str, float]:
    if not nodeids:
        return {}

    durations: dict[str, float] = {}
    for nodeid in nodeids:
        with tempfile.TemporaryDirectory(prefix="fast-test-budget-") as temp_dir:
            report_path = Path(temp_dir) / "report.xml"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:xdist",
                    "--rootdir",
                    str(repo_root),
                    "-o",
                    "junit_duration_report=call",
                    "--junitxml",
                    str(report_path),
                    nodeid,
                ],
                cwd=repo_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if completed.returncode not in (0, 1) or not report_path.exists():
                continue
            tree = ET.parse(report_path)
            testcase = next(tree.iterfind(".//testcase"), None)
            if testcase is None:
                continue
            normalized_nodeid = normalize_testcase_nodeid(testcase, repo_root=repo_root)
            if normalized_nodeid != nodeid:
                continue
            durations[nodeid] = _parse_positive_float(
                str(testcase.attrib.get("time") or "0"),
                default=0.0,
            )
    return durations


def verify_fast_test_budget_offenders(
    offenders: list[FastTestBudgetOffender],
    *,
    threshold_seconds: float,
    repo_root: Path,
    duration_collector: DurationCollector = _collect_call_durations_from_pytest,
    max_nodeids: Optional[int] = None,
) -> list[FastTestBudgetOffender]:
    if not offenders:
        return []

    verification_candidates = (
        offenders[:max_nodeids] if max_nodeids is not None else offenders
    )
    verified_durations = duration_collector(
        [offender.nodeid for offender in verification_candidates],
        repo_root.resolve(),
    )
    verified: list[FastTestBudgetOffender] = []
    for offender in offenders:
        duration_seconds = verified_durations.get(
            offender.nodeid,
            offender.duration_seconds,
        )
        if duration_seconds <= threshold_seconds:
            continue
        verified.append(
            FastTestBudgetOffender(
                nodeid=offender.nodeid,
                duration_seconds=duration_seconds,
                outcome=offender.outcome,
            )
        )
    verified.sort(key=lambda item: item.duration_seconds, reverse=True)
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report over-budget tests from a pytest JUnit XML report."
    )
    parser.add_argument("report_path", type=Path)
    parser.add_argument("--max-duration", default="1.0")
    parser.add_argument("--max-report", default="20")
    parser.add_argument("--selected-nodeids", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--fail-on-violation", action="store_true")
    parser.add_argument("--verify-nodeids", action="store_true")
    args = parser.parse_args(argv)

    threshold_seconds = _parse_positive_float(args.max_duration, default=1.0)
    report_limit = _parse_positive_int(args.max_report, default=20)
    selected_nodeids = _load_selected_nodeids(args.selected_nodeids)
    offenders = collect_fast_test_budget_offenders(
        args.report_path,
        threshold_seconds=threshold_seconds,
        selected_nodeids=selected_nodeids,
        repo_root=args.repo_root,
    )
    if args.verify_nodeids and offenders:
        verification_limit = None if args.fail_on_violation else report_limit
        offenders = verify_fast_test_budget_offenders(
            offenders,
            threshold_seconds=threshold_seconds,
            repo_root=args.repo_root,
            max_nodeids=verification_limit,
        )
    all_durations: list[float] = []
    tree = ET.parse(args.report_path)
    for testcase in tree.iterfind(".//testcase"):
        outcome = _testcase_outcome(testcase)
        if outcome == "skipped":
            continue
        normalized_nodeid = normalize_testcase_nodeid(
            testcase, repo_root=args.repo_root.resolve()
        )
        if selected_nodeids is not None and normalized_nodeid not in selected_nodeids:
            continue
        all_durations.append(
            _parse_positive_float(
                str(testcase.attrib.get("time") or "0"),
                default=0.0,
            )
        )
    for line in build_report_lines(
        offenders,
        threshold_seconds=threshold_seconds,
        report_limit=report_limit,
        summary=summarize_durations(all_durations),
    ):
        print(line)
    if offenders and args.fail_on_violation:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
