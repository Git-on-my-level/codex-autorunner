from __future__ import annotations

from pathlib import Path

from scripts.report_fast_test_budget import (
    FastTestBudgetOffender,
    build_report_lines,
    collect_fast_test_budget_offenders,
)


def _write_junit_report(path: Path, xml_text: str) -> None:
    path.write_text(xml_text, encoding="utf-8")


def test_collect_fast_test_budget_offenders_filters_and_sorts(tmp_path: Path) -> None:
    report_path = tmp_path / "report.xml"
    _write_junit_report(
        report_path,
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" failures="1" skipped="1">
    <testcase classname="tests.test_alpha" file="tests/test_alpha.py" name="test_fast" time="0.12" />
    <testcase classname="tests.test_beta" file="tests/test_beta.py" name="test_slow" time="1.75">
      <failure message="boom">boom</failure>
    </testcase>
    <testcase classname="tests.test_gamma" name="test_medium" time="0.75">
      <skipped message="skip" />
    </testcase>
  </testsuite>
</testsuites>
""",
    )

    offenders = collect_fast_test_budget_offenders(
        report_path,
        threshold_seconds=0.5,
    )

    assert [
        (item.nodeid, item.duration_seconds, item.outcome) for item in offenders
    ] == [
        ("tests/test_beta.py::test_slow", 1.75, "failed"),
        ("tests.test_gamma::test_medium", 0.75, "skipped"),
    ]


def test_build_report_lines_caps_output_and_reports_remaining() -> None:
    offenders = [
        FastTestBudgetOffender("a::test_one", 2.0, "passed"),
        FastTestBudgetOffender("b::test_two", 1.5, "failed"),
        FastTestBudgetOffender("c::test_three", 1.0, "skipped"),
    ]

    lines = build_report_lines(
        offenders,
        threshold_seconds=0.5,
        report_limit=2,
    )

    assert lines[0] == "Slow fast tests: 3 tests exceeded the 0.50s budget."
    assert "a::test_one" in lines[2]
    assert "b::test_two" in lines[3]
    assert lines[4] == "  ... 1 more over-budget fast tests not shown"
