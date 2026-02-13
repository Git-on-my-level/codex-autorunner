from __future__ import annotations

from codex_autorunner.core.flows.failure_diagnostics import (
    _derive_failure_reason_code,
    build_failure_payload,
)
from codex_autorunner.core.flows.models import FailureReasonCode, FlowEventType
from codex_autorunner.core.flows.store import FlowStore


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
