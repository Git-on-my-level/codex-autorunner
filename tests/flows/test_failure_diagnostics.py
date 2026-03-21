from __future__ import annotations

from codex_autorunner.core.flows.failure_diagnostics import (
    _derive_failure_reason_code,
    build_failure_payload,
    get_terminal_failure_reason_code,
)
from codex_autorunner.core.flows.models import (
    FailureReasonCode,
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
)
from codex_autorunner.core.flows.store import FlowStore


def _build_record(
    *,
    state: dict | None = None,
    error_message: str | None = None,
    status: FlowRunStatus = FlowRunStatus.FAILED,
) -> FlowRunRecord:
    return FlowRunRecord(
        id="run-1",
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        current_step=None,
        stop_requested=False,
        created_at="2026-03-21T00:00:00Z",
        started_at="2026-03-21T00:00:00Z",
        finished_at="2026-03-21T00:00:10Z",
        error_message=error_message,
        metadata={},
    )


def test_build_failure_payload_uses_newest_app_server_events(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    record = store.create_flow_run(
        run_id="run-failure-diag",
        flow_type="ticket_flow",
        input_data={},
    )

    for idx in range(250):
        store.create_event(
            event_id=f"evt-{idx}",
            run_id=record.id,
            event_type=FlowEventType.APP_SERVER_EVENT,
            data={
                "message": {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "command": f"cmd-{idx}",
                            "exitCode": idx,
                            "stderr": f"stderr-{idx}",
                        }
                    },
                }
            },
        )

    payload = build_failure_payload(record, store=store)

    assert payload["last_command"] == "cmd-249"
    assert payload["exit_code"] == 249
    assert payload["stderr_tail"] == "stderr-249"
    assert "failure_reason_code" in payload
    assert payload["failure_reason_code"] == "unknown"
    assert "last_event_seq" in payload
    assert payload["last_event_seq"] is not None


def test_derive_failure_reason_code_oom() -> None:
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Process killed by OOM", note=None
        )
        == FailureReasonCode.OOM_KILLED
    )
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Memory allocation failed", note=None
        )
        == FailureReasonCode.OOM_KILLED
    )
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Something happened", note=None, exit_code=137
        )
        == FailureReasonCode.OOM_KILLED
    )


def test_derive_failure_reason_code_network() -> None:
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Connection error", note=None
        )
        == FailureReasonCode.NETWORK_ERROR
    )
    assert (
        _derive_failure_reason_code(state={}, error_message="Network error", note=None)
        == FailureReasonCode.NETWORK_ERROR
    )
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Rate limit exceeded (429)", note=None
        )
        == FailureReasonCode.NETWORK_ERROR
    )


def test_derive_failure_reason_code_preflight() -> None:
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Preflight check failed", note=None
        )
        == FailureReasonCode.PREFLIGHT_ERROR
    )
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Bootstrap failed: missing config", note=None
        )
        == FailureReasonCode.PREFLIGHT_ERROR
    )


def test_derive_failure_reason_code_timeout() -> None:
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Operation timed out", note=None
        )
        == FailureReasonCode.TIMEOUT
    )


def test_derive_failure_reason_code_worker_dead() -> None:
    assert (
        _derive_failure_reason_code(state={}, error_message=None, note="worker-dead")
        == FailureReasonCode.WORKER_DEAD
    )


def test_derive_failure_reason_code_worker_dead_from_error_message() -> None:
    assert (
        _derive_failure_reason_code(
            state={},
            error_message="Worker died (status=dead, pid=123, reason: lost worker)",
            note=None,
        )
        == FailureReasonCode.WORKER_DEAD
    )


def test_derive_failure_reason_code_agent_crash() -> None:
    assert (
        _derive_failure_reason_code(
            state={}, error_message="Agent crash detected", note=None
        )
        == FailureReasonCode.AGENT_CRASH
    )


def test_derive_failure_reason_code_note_takes_precedence() -> None:
    assert (
        _derive_failure_reason_code(
            state={},
            error_message="Worker died (status=dead, pid=123)",
            note="worker-dead",
        )
        == FailureReasonCode.WORKER_DEAD
    )
    assert (
        _derive_failure_reason_code(
            state={},
            error_message="Worker died unexpectedly",
            note="worker-dead",
        )
        == FailureReasonCode.WORKER_DEAD
    )


def test_get_terminal_failure_reason_code_prefers_canonical_failure_payload() -> None:
    record = _build_record(
        state={
            "failure": {
                "failure_reason_code": "worker_dead",
                "failure_class": "error",
            },
            "ticket_engine": {"reason_code": "timeout"},
        },
        error_message="Operation timed out",
    )

    assert get_terminal_failure_reason_code(record) == FailureReasonCode.WORKER_DEAD


def test_get_terminal_failure_reason_code_characterizes_terminal_paths() -> None:
    cases = [
        (
            _build_record(
                error_message="Worker died (status=dead, pid=123, reason: lost worker)"
            ),
            FailureReasonCode.WORKER_DEAD,
        ),
        (
            _build_record(error_message="Operation timed out after 30s"),
            FailureReasonCode.TIMEOUT,
        ),
        (
            _build_record(error_message="Preflight check failed"),
            FailureReasonCode.PREFLIGHT_ERROR,
        ),
        (
            _build_record(error_message="Connection error to backend"),
            FailureReasonCode.NETWORK_ERROR,
        ),
        (
            _build_record(
                state={"ticket_engine": {"reason_code": "user_stop"}},
                status=FlowRunStatus.STOPPED,
            ),
            FailureReasonCode.USER_STOP,
        ),
        (
            _build_record(error_message="Unhandled exception: failure"),
            FailureReasonCode.UNCAUGHT_EXCEPTION,
        ),
    ]

    for record, expected in cases:
        assert get_terminal_failure_reason_code(record) == expected


def test_get_terminal_failure_reason_code_uses_legacy_failure_class_mapping() -> None:
    record = _build_record(
        state={"failure": {"failure_class": "network"}},
        error_message=None,
    )

    assert get_terminal_failure_reason_code(record) == FailureReasonCode.NETWORK_ERROR
