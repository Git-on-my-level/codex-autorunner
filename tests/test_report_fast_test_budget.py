from __future__ import annotations

from pathlib import Path

from scripts.report_fast_test_budget import (
    FastTestBudgetOffender,
    build_report_lines,
    collect_fast_test_budget_offenders,
    summarize_durations,
    verify_fast_test_budget_offenders,
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


def test_collect_fast_test_budget_offenders_filters_by_selected_nodeids(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.xml"
    _write_junit_report(
        report_path,
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2">
    <testcase classname="tests.pkg.test_alpha.TestSuite" name="test_kept" time="1.25" />
    <testcase classname="tests.pkg.test_alpha.TestSuite" name="test_filtered" time="2.50" />
  </testsuite>
</testsuites>
""",
    )
    pkg_dir = tmp_path / "tests" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "test_alpha.py").write_text(
        "class TestSuite:\n    pass\n", encoding="utf-8"
    )

    offenders = collect_fast_test_budget_offenders(
        report_path,
        threshold_seconds=0.5,
        selected_nodeids={"tests/pkg/test_alpha.py::TestSuite::test_kept"},
        repo_root=tmp_path,
    )

    assert [(item.nodeid, item.duration_seconds) for item in offenders] == [
        ("tests/pkg/test_alpha.py::TestSuite::test_kept", 1.25),
    ]


def test_summarize_durations_reports_percentiles() -> None:
    summary = summarize_durations([0.1, 0.2, 0.3, 1.0, 2.0])

    assert summary["count"] == 5
    assert float(summary["p80"]) > 1.0
    assert float(summary["p99"]) > float(summary["p95"])
    assert summary["max"] == 2.0


def test_verify_fast_test_budget_offenders_filters_by_verified_call_duration() -> None:
    offenders = [
        FastTestBudgetOffender("a::test_fast_now", 2.0, "passed"),
        FastTestBudgetOffender("b::test_still_slow", 1.5, "failed"),
        FastTestBudgetOffender("c::test_fallback", 1.25, "passed"),
    ]

    verified = verify_fast_test_budget_offenders(
        offenders,
        threshold_seconds=1.0,
        repo_root=Path.cwd(),
        duration_collector=lambda nodeids, repo_root: {
            "a::test_fast_now": 0.4,
            "b::test_still_slow": 1.1,
        },
    )

    assert verified == [
        FastTestBudgetOffender("c::test_fallback", 1.25, "passed"),
        FastTestBudgetOffender("b::test_still_slow", 1.1, "failed"),
    ]


def test_verify_fast_test_budget_offenders_limits_verified_nodeids() -> None:
    offenders = [
        FastTestBudgetOffender("a::test_slowest", 4.0, "passed"),
        FastTestBudgetOffender("b::test_second", 3.0, "passed"),
        FastTestBudgetOffender("c::test_third", 2.0, "passed"),
    ]
    seen_nodeids: list[str] = []

    def _collector(nodeids: list[str], repo_root: Path) -> dict[str, float]:
        _ = repo_root
        seen_nodeids.extend(nodeids)
        return {
            "a::test_slowest": 0.5,
            "b::test_second": 2.5,
        }

    verified = verify_fast_test_budget_offenders(
        offenders,
        threshold_seconds=1.0,
        repo_root=Path.cwd(),
        duration_collector=_collector,
        max_nodeids=2,
    )

    assert seen_nodeids == ["a::test_slowest", "b::test_second"]
    assert verified == [
        FastTestBudgetOffender("b::test_second", 2.5, "passed"),
        FastTestBudgetOffender("c::test_third", 2.0, "passed"),
    ]
