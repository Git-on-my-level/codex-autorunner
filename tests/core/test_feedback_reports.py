from __future__ import annotations

import pytest

from codex_autorunner.core.feedback_reports import (
    FeedbackReportStore,
    normalize_feedback_report_input,
)


def test_normalize_feedback_report_input_canonicalizes_fields() -> None:
    normalized = normalize_feedback_report_input(
        report_id=" report-001 ",
        repo_id=" repo-123 ",
        thread_target_id=" thread-456 ",
        report_kind="  Review  Risk  ",
        title="  Missing   rollback coverage  ",
        body="  First line  \r\nSecond line   \r\n",
        evidence=[
            {"line": 12, "path": "src/app.py"},
            "  stack trace  ",
            {"path": "src/app.py", "line": 12},
        ],
        confidence="0.75",
        source_kind="  manual review ",
        source_id=" dispatch-1 ",
        status="  open ",
    )

    assert normalized.report_id == "report-001"
    assert normalized.repo_id == "repo-123"
    assert normalized.thread_target_id == "thread-456"
    assert normalized.report_kind == "Review Risk"
    assert normalized.title == "Missing rollback coverage"
    assert normalized.body == "First line\nSecond line"
    assert normalized.evidence == [
        "stack trace",
        {"line": 12, "path": "src/app.py"},
        {"line": 12, "path": "src/app.py"},
    ]
    assert normalized.confidence == 0.75
    assert normalized.source_kind == "manual review"
    assert normalized.source_id == "dispatch-1"
    assert normalized.status == "open"
    assert len(normalized.dedupe_key) == 32


def test_equivalent_normalized_report_content_shares_dedupe_key() -> None:
    left = normalize_feedback_report_input(
        repo_id="repo-1",
        thread_target_id="thread-1",
        report_kind="risk",
        title="Missing test coverage",
        body="Needs a regression test.\n",
        evidence=[
            {"path": "tests/core/test_feature.py", "line": 18},
            "pytest -k feature",
        ],
        confidence=0.5,
        source_kind="manual",
        source_id="dispatch-1",
    )
    right = normalize_feedback_report_input(
        repo_id=" repo-1 ",
        thread_target_id="thread-1",
        report_kind=" risk ",
        title="  Missing   test coverage ",
        body="  Needs a regression test.\r\n",
        evidence=[
            " pytest -k feature ",
            {"line": 18, "path": "tests/core/test_feature.py"},
        ],
        confidence="0.500",
        source_kind="manual",
        source_id="dispatch-2",
        status="open",
    )

    assert left.dedupe_key == right.dedupe_key


def test_feedback_report_store_create_get_and_restart_safe_lookup(tmp_path) -> None:
    hub_root = tmp_path / "hub"
    created = FeedbackReportStore(hub_root).create_report(
        report_id="report-001",
        repo_id="repo-1",
        thread_target_id="thread-1",
        report_kind="risk",
        title="Missing test coverage",
        body="Needs a regression test.",
        evidence=[{"path": "tests/core/test_feature.py", "line": 18}],
        confidence=0.5,
        source_kind="manual",
        source_id="dispatch-1",
    )

    reopened_store = FeedbackReportStore(hub_root)
    fetched = reopened_store.get_report("report-001")

    assert created.report_id == "report-001"
    assert fetched is not None
    assert fetched.to_dict() == created.to_dict()
    assert fetched.dedupe_key == created.dedupe_key


def test_feedback_report_store_list_reports_filters_by_dedupe_key_and_limit(
    tmp_path,
) -> None:
    hub_root = tmp_path / "hub"
    store = FeedbackReportStore(hub_root)
    duplicate_one = store.create_report(
        report_id="report-001",
        repo_id="repo-1",
        thread_target_id="thread-1",
        report_kind="risk",
        title="Missing test coverage",
        body="Needs a regression test.",
        evidence=[{"path": "tests/core/test_feature.py", "line": 18}],
        confidence=0.5,
        source_kind="manual",
        source_id="dispatch-1",
    )
    duplicate_two = store.create_report(
        report_id="report-002",
        repo_id="repo-1",
        thread_target_id="thread-1",
        report_kind=" risk ",
        title=" Missing   test coverage ",
        body="Needs a regression test.\n",
        evidence=[{"line": 18, "path": "tests/core/test_feature.py"}],
        confidence="0.500",
        source_kind="manual",
        source_id="dispatch-2",
    )
    store.create_report(
        report_id="report-003",
        repo_id="repo-1",
        thread_target_id="thread-1",
        report_kind="idea",
        title="Add timeline drilldown",
        body="Would help triage repeated alerts.",
        evidence=[],
        source_kind="manual",
        source_id="dispatch-3",
        status="resolved",
    )

    duplicates = store.list_reports(
        repo_id="repo-1",
        dedupe_key=duplicate_one.dedupe_key,
        limit=10,
    )
    limited = store.list_reports(repo_id="repo-1", limit=1)
    resolved = store.list_reports(repo_id="repo-1", status=" resolved ", limit=10)

    assert duplicate_one.dedupe_key == duplicate_two.dedupe_key
    assert [report.report_id for report in duplicates] == ["report-002", "report-001"]
    assert [report.report_id for report in limited] == ["report-003"]
    assert [report.report_id for report in resolved] == ["report-003"]


def test_feedback_report_store_rejects_invalid_confidence(tmp_path) -> None:
    store = FeedbackReportStore(tmp_path / "hub")

    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        store.create_report(
            report_kind="risk",
            title="Broken confidence",
            body="Out of range confidence should fail.",
            source_kind="manual",
            confidence=1.5,
        )
