from __future__ import annotations

import pytest

from codex_autorunner.core.pma_domain.constants import (
    NOTICE_KIND_ESCALATION,
    NOTICE_KIND_NOOP,
    NOTICE_KIND_TERMINAL_FOLLOWUP,
    SOURCE_KIND_MANAGED_THREAD_COMPLETED,
    SUPPRESSED_REASON_DUPLICATE_NOOP,
)
from codex_autorunner.core.pma_domain.models import (
    PublishNoticeContext,
    PublishSuppressionDecision,
)
from codex_autorunner.core.pma_domain.publish_policy import (
    build_publish_notice_message,
    classify_notice_kind,
    evaluate_publish_suppression,
    is_noop_duplicate_message,
)


class TestIsNoopDuplicateMessage:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("Already handled. No action needed.", True),
            ("Duplicate — already handled, no action.", True),
            ("already handled, no action.", True),
            ("Already  handled   no  action", True),
            ("Thread already handled. No action required.", True),
            ("", False),
            ("Some normal message", False),
            ("Already handled", False),
            ("No action", False),
            ("Task completed successfully", False),
            (None, False),
        ],
    )
    def test_noop_detection(self, message: str | None, expected: bool) -> None:
        assert is_noop_duplicate_message(message or "") == expected


class TestClassifyNoticeKind:
    def test_ok_with_noop_message_is_noop(self) -> None:
        assert (
            classify_notice_kind(
                source_kind="managed_thread_completed",
                status="ok",
                message_text="Already handled. No action needed.",
            )
            == NOTICE_KIND_NOOP
        )

    def test_ok_with_normal_message_is_terminal_followup(self) -> None:
        assert (
            classify_notice_kind(
                source_kind="managed_thread_completed",
                status="ok",
                message_text="Changes pushed successfully.",
            )
            == NOTICE_KIND_TERMINAL_FOLLOWUP
        )

    def test_error_is_escalation(self) -> None:
        assert (
            classify_notice_kind(
                source_kind="managed_thread_completed",
                status="error",
                message_text="Turn failed.",
            )
            == NOTICE_KIND_ESCALATION
        )

    def test_unknown_source_returns_source_kind(self) -> None:
        assert (
            classify_notice_kind(
                source_kind="timer",
                status="running",
                message_text="progress update",
            )
            == "timer"
        )


class TestEvaluatePublishSuppression:
    def test_suppresses_duplicate_noop_to_same_binding(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is True
        assert decision.reason == SUPPRESSED_REASON_DUPLICATE_NOOP
        assert decision.notice_kind == NOTICE_KIND_NOOP

    def test_does_not_suppress_when_target_differs(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=False,
        )
        assert decision.suppressed is False

    def test_does_not_suppress_without_managed_thread(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id=None,
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is False

    def test_does_not_suppress_non_completion_source(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind="automation",
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is False

    def test_does_not_suppress_non_noop_message(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Changes pushed to branch.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is False

    def test_does_not_suppress_error_status(self) -> None:
        decision = evaluate_publish_suppression(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            message_text="Already handled. No action needed.",
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
        )
        assert decision.suppressed is True


class TestPublishSuppressionDecisionModel:
    def test_not_suppressed_factory(self) -> None:
        decision = PublishSuppressionDecision.not_suppressed(
            notice_kind=NOTICE_KIND_TERMINAL_FOLLOWUP
        )
        assert decision.suppressed is False
        assert decision.reason == ""
        assert decision.notice_kind == NOTICE_KIND_TERMINAL_FOLLOWUP

    def test_duplicate_noop_factory(self) -> None:
        decision = PublishSuppressionDecision.duplicate_noop(
            notice_kind=NOTICE_KIND_NOOP
        )
        assert decision.suppressed is True
        assert decision.reason == SUPPRESSED_REASON_DUPLICATE_NOOP
        assert decision.notice_kind == NOTICE_KIND_NOOP

    def test_evaluate_suppressed(self) -> None:
        decision = PublishSuppressionDecision.evaluate(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
            notice_kind=NOTICE_KIND_NOOP,
        )
        assert decision.suppressed is True

    def test_evaluate_not_suppressed(self) -> None:
        decision = PublishSuppressionDecision.evaluate(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            managed_thread_id="thread-1",
            target_matches_thread_binding=True,
            notice_kind=NOTICE_KIND_TERMINAL_FOLLOWUP,
        )
        assert decision.suppressed is False

    def test_evaluate_no_thread_id(self) -> None:
        decision = PublishSuppressionDecision.evaluate(
            source_kind=SOURCE_KIND_MANAGED_THREAD_COMPLETED,
            managed_thread_id=None,
            target_matches_thread_binding=True,
            notice_kind=NOTICE_KIND_NOOP,
        )
        assert decision.suppressed is False


class TestPublishNoticeContextNoticeKind:
    def test_ok_noop_message(self) -> None:
        ctx = PublishNoticeContext(
            trigger="managed_thread_completed",
            status="ok",
            correlation_id="corr-1",
            output="Already handled. No action needed.",
        )
        assert ctx.notice_kind() == NOTICE_KIND_NOOP

    def test_ok_normal_message(self) -> None:
        ctx = PublishNoticeContext(
            trigger="managed_thread_completed",
            status="ok",
            correlation_id="corr-1",
            output="Done",
        )
        assert ctx.notice_kind() == NOTICE_KIND_TERMINAL_FOLLOWUP

    def test_error_status(self) -> None:
        ctx = PublishNoticeContext(
            trigger="managed_thread_completed",
            status="error",
            correlation_id="corr-1",
            detail="timeout",
        )
        assert ctx.notice_kind() == NOTICE_KIND_ESCALATION


class TestBuildPublishNoticeMessage:
    def test_ok_status_with_output(self) -> None:
        ctx = PublishNoticeContext(
            trigger="turn_completed",
            status="ok",
            correlation_id="corr-1",
            repo_id="repo-a",
            run_id="run-1",
            thread_id="thread-1",
            output="All done",
        )
        msg = build_publish_notice_message(ctx)
        assert "PMA update (turn_completed)" in msg
        assert "repo_id: repo-a" in msg
        assert "run_id: run-1" in msg
        assert "thread_id: thread-1" in msg
        assert "correlation_id: corr-1" in msg
        assert "All done" in msg
        assert "error" not in msg

    def test_ok_status_without_output(self) -> None:
        ctx = PublishNoticeContext(
            trigger="turn_completed",
            status="ok",
            correlation_id="corr-2",
        )
        msg = build_publish_notice_message(ctx)
        assert "Turn completed with no assistant output." in msg

    def test_error_status_with_detail(self) -> None:
        ctx = PublishNoticeContext(
            trigger="turn_failed",
            status="error",
            correlation_id="corr-3",
            detail="timeout exceeded",
        )
        msg = build_publish_notice_message(ctx)
        assert "status: error" in msg
        assert "error: timeout exceeded" in msg
        assert "next_action" in msg

    def test_error_status_without_detail(self) -> None:
        ctx = PublishNoticeContext(
            trigger="turn_failed",
            status="error",
            correlation_id="corr-4",
        )
        msg = build_publish_notice_message(ctx)
        assert "error: Turn failed without detail." in msg

    def test_includes_token_usage_footer(self) -> None:
        ctx = PublishNoticeContext(
            trigger="managed_thread_completed",
            status="ok",
            correlation_id="corr-5",
            output="Done",
            token_usage_footer="Token usage: total 100 input 50 output 50",
        )
        msg = build_publish_notice_message(ctx)
        assert "Token usage: total 100 input 50 output 50" in msg

    def test_omits_token_usage_footer_when_none(self) -> None:
        ctx = PublishNoticeContext(
            trigger="managed_thread_completed",
            status="ok",
            correlation_id="corr-6",
            output="Done",
            token_usage_footer=None,
        )
        msg = build_publish_notice_message(ctx)
        assert "Token usage" not in msg

    def test_omits_optional_fields_when_none(self) -> None:
        ctx = PublishNoticeContext(
            trigger="automation",
            status="ok",
            correlation_id="corr-7",
        )
        msg = build_publish_notice_message(ctx)
        assert "repo_id" not in msg
        assert "run_id" not in msg
        assert "thread_id" not in msg

    def test_error_includes_token_usage_footer(self) -> None:
        ctx = PublishNoticeContext(
            trigger="turn_failed",
            status="error",
            correlation_id="corr-8",
            detail="timeout",
            token_usage_footer="Token usage: total 200 input 100 output 100",
        )
        msg = build_publish_notice_message(ctx)
        assert "Token usage: total 200 input 100 output 100" in msg
        assert "error: timeout" in msg
