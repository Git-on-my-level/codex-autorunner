from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from codex_autorunner.surfaces.web.routes.pma_routes.managed_thread_runtime_payloads import (
    MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
    build_accepted_send_payload,
    build_archived_thread_payload,
    build_execution_result_payload,
    build_execution_setup_error_payload,
    build_interrupt_failure_payload,
    build_not_active_thread_payload,
    build_queued_send_payload,
    build_running_turn_exists_payload,
    build_started_execution_error_payload,
    normalize_busy_policy,
    sanitize_managed_thread_result_error,
)


class TestSanitizeManagedThreadResultError:
    @pytest.mark.parametrize(
        "input_detail",
        ["PMA chat timed out", "Runtime thread timed out"],
    )
    def test_timeout_variants_sanitized(self, input_detail: str) -> None:
        assert (
            sanitize_managed_thread_result_error(input_detail) == "PMA chat timed out"
        )

    @pytest.mark.parametrize(
        "input_detail",
        ["PMA chat interrupted", "Runtime thread interrupted"],
    )
    def test_interrupt_variants_sanitized(self, input_detail: str) -> None:
        assert (
            sanitize_managed_thread_result_error(input_detail) == "PMA chat interrupted"
        )

    def test_unknown_error_sanitized(self) -> None:
        assert (
            sanitize_managed_thread_result_error("something went wrong")
            == MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
        )

    def test_none_input_sanitized(self) -> None:
        assert (
            sanitize_managed_thread_result_error(None)
            == MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
        )

    def test_empty_input_sanitized(self) -> None:
        assert (
            sanitize_managed_thread_result_error("")
            == MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
        )


class TestNormalizeBusyPolicy:
    def test_none_defaults_to_queue(self) -> None:
        assert normalize_busy_policy(None) == "queue"

    @pytest.mark.parametrize("value", ["queue", "interrupt", "reject"])
    def test_valid_values_accepted(self, value: str) -> None:
        assert normalize_busy_policy(value) == value

    @pytest.mark.parametrize("value", ["QUEUE", "Interrupt", "REJECT"])
    def test_case_insensitive(self, value: str) -> None:
        assert normalize_busy_policy(value) == value.lower()

    def test_invalid_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            normalize_busy_policy("block")
        assert exc_info.value.status_code == 400
        assert "busy_policy must be one of" in exc_info.value.detail


class TestBuildInterruptFailurePayload:
    def test_contains_required_keys(self) -> None:
        payload = build_interrupt_failure_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            detail="interrupt failed",
            delivery_payload={"delivered_message": "hello"},
        )
        for key in (
            "status",
            "send_state",
            "interrupt_state",
            "execution_state",
            "reason",
            "detail",
            "next_step",
            "managed_thread_id",
            "managed_turn_id",
            "active_managed_turn_id",
            "backend_thread_id",
            "assistant_text",
            "error",
        ):
            assert key in payload

    def test_delivery_payload_spread(self) -> None:
        payload = build_interrupt_failure_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            detail="fail",
            delivery_payload={"delivered_message": "hello", "extra": True},
        )
        assert payload["delivered_message"] == "hello"
        assert payload["extra"] is True


class TestBuildArchivedThreadPayload:
    def test_contains_required_keys(self) -> None:
        payload = build_archived_thread_payload(
            managed_thread_id="t-1",
            backend_thread_id="bt-1",
        )
        for key in (
            "status",
            "send_state",
            "reason",
            "detail",
            "next_step",
            "managed_thread_id",
            "managed_turn_id",
            "backend_thread_id",
            "assistant_text",
            "error",
        ):
            assert key in payload
        assert payload["status"] == "error"
        assert payload["reason"] == "thread_archived"
        assert payload["managed_turn_id"] is None


class TestBuildNotActiveThreadPayload:
    def test_archived_status_detail(self) -> None:
        exc = SimpleNamespace(status="archived")
        payload = build_not_active_thread_payload(
            managed_thread_id="t-1",
            backend_thread_id="bt-1",
            exc=exc,
        )
        assert "archived" in payload["detail"]

    def test_non_archived_status_detail(self) -> None:
        exc = SimpleNamespace(status="paused")
        payload = build_not_active_thread_payload(
            managed_thread_id="t-1",
            backend_thread_id="bt-1",
            exc=exc,
        )
        assert "not active" in payload["detail"]


class TestBuildRunningTurnExistsPayload:
    def test_contains_running_turn_info(self) -> None:
        payload = build_running_turn_exists_payload(
            managed_thread_id="t-1",
            backend_thread_id="bt-1",
            running_turn={"managed_turn_id": "turn-active"},
        )
        assert payload["managed_turn_id"] == "turn-active"
        assert payload["send_state"] == "already_in_flight"
        assert payload["reason"] == "running_turn_exists"

    def test_no_running_turn(self) -> None:
        payload = build_running_turn_exists_payload(
            managed_thread_id="t-1",
            backend_thread_id="bt-1",
            running_turn=None,
        )
        assert payload["managed_turn_id"] is None


class TestBuildExecutionSetupErrorPayload:
    def test_contains_required_keys(self) -> None:
        payload = build_execution_setup_error_payload(
            managed_thread_id="t-1",
            backend_thread_id="bt-1",
            delivery_payload={"delivered_message": "msg"},
        )
        assert payload["status"] == "error"
        assert payload["error"] == MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
        assert payload["delivered_message"] == "msg"


class TestBuildStartedExecutionErrorPayload:
    def test_custom_error_message(self) -> None:
        payload = build_started_execution_error_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            error="custom error",
            delivery_payload={"delivered_message": "msg"},
        )
        assert payload["error"] == "custom error"
        assert payload["managed_turn_id"] == "turn-1"


class TestBuildQueuedSendPayload:
    def test_without_notification(self) -> None:
        payload = build_queued_send_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            delivery_payload={"delivered_message": "msg"},
            queue_depth=2,
            active_managed_turn_id="turn-running",
        )
        assert payload["status"] == "ok"
        assert payload["send_state"] == "queued"
        assert payload["queue_depth"] == 2
        assert "notification" not in payload

    def test_with_notification(self) -> None:
        payload = build_queued_send_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            delivery_payload={"delivered_message": "msg"},
            queue_depth=1,
            active_managed_turn_id=None,
            notification={"event": "terminal"},
        )
        assert payload["notification"] == {"event": "terminal"}


class TestBuildAcceptedSendPayload:
    def test_without_notification(self) -> None:
        payload = build_accepted_send_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            delivery_payload={"delivered_message": "msg"},
        )
        assert payload["status"] == "ok"
        assert payload["send_state"] == "accepted"
        assert payload["execution_state"] == "running"
        assert "notification" not in payload

    def test_with_notification(self) -> None:
        payload = build_accepted_send_payload(
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            delivery_payload={"delivered_message": "msg"},
            notification={"event": "terminal"},
        )
        assert payload["notification"] == {"event": "terminal"}


class TestBuildExecutionResultPayload:
    def test_ok_result(self) -> None:
        payload = build_execution_result_payload(
            status="ok",
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            assistant_text="done",
            error=None,
            response_payload={"delivered_message": "msg"},
        )
        assert payload["status"] == "ok"
        assert payload["assistant_text"] == "done"
        assert payload["error"] is None
        assert payload["delivered_message"] == "msg"

    def test_error_result(self) -> None:
        payload = build_execution_result_payload(
            status="error",
            managed_thread_id="t-1",
            managed_turn_id="turn-1",
            backend_thread_id="bt-1",
            assistant_text="",
            error="timeout",
            response_payload={"delivered_message": "msg"},
        )
        assert payload["status"] == "error"
        assert payload["error"] == "timeout"
