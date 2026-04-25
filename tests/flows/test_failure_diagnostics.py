from __future__ import annotations

from codex_autorunner.core.flows.failure_diagnostics import (
    ReconcileContext,
    _derive_failure_reason_code,
    build_failure_event_data,
    build_failure_payload,
    ensure_failure_payload,
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
        store.create_telemetry(
            telemetry_id=f"tel-{idx}",
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


def test_build_failure_payload_prefers_latest_timestamp_across_tables(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    record = store.create_flow_run(
        run_id="run-failure-diag-ts",
        flow_type="ticket_flow",
        input_data={},
    )
    store.create_event(
        event_id="evt-older",
        run_id=record.id,
        event_type=FlowEventType.STEP_PROGRESS,
        data={"message": "older"},
    )
    store.create_telemetry(
        telemetry_id="tel-newer",
        run_id=record.id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": "cmd-newer",
                        "exitCode": 7,
                        "stderr": "stderr-newer",
                    }
                },
            }
        },
    )

    conn = store._get_conn()
    conn.execute(
        "UPDATE flow_events SET timestamp = ? WHERE id = ?",
        ("2026-03-21T00:00:20Z", "evt-older"),
    )
    conn.execute(
        "UPDATE flow_telemetry SET timestamp = ? WHERE id = ?",
        ("2026-03-21T00:00:30Z", "tel-newer"),
    )

    payload = build_failure_payload(record, store=store)

    assert payload["last_event_at"] == "2026-03-21T00:00:30Z"


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


def test_build_failure_payload_without_reconcile_context() -> None:
    record = _build_record(error_message="something broke")
    payload = build_failure_payload(record, error_message="something broke")

    assert payload["exit_code"] is None
    assert "crash" not in payload
    assert payload["failure_reason_code"] == "uncaught_exception"


def test_build_failure_payload_with_reconcile_context_fills_worker_fields() -> None:
    record = _build_record(error_message="Process OOM killed")
    ctx = ReconcileContext(
        worker_exit_code=137,
        crash_info={
            "exit_code": 137,
            "exception": "OOM",
            "timestamp": "2026-03-21T00:01:00Z",
        },
    )
    payload = build_failure_payload(
        record, error_message="Process OOM killed", reconcile_context=ctx
    )

    assert payload["exit_code"] == 137
    assert payload["crash"] == ctx.crash_info
    assert payload["failure_reason_code"] == "oom_killed"


def test_reconcile_context_stderr_tail_fallback_when_no_error_message() -> None:
    record = _build_record(error_message=None)
    ctx = ReconcileContext(
        worker_exit_code=1,
        worker_stderr_tail="segfault output\n",
    )
    payload = build_failure_payload(record, error_message=None, reconcile_context=ctx)

    assert payload["exit_code"] == 1
    assert payload["stderr_tail"] == "segfault output"


def test_build_failure_payload_reconcile_context_does_not_overwrite_store_exit_code(
    tmp_path,
) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    record = store.create_flow_run(
        run_id="run-ctx-priority",
        flow_type="ticket_flow",
        input_data={},
    )
    store.create_telemetry(
        telemetry_id="tel-1",
        run_id=record.id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": "true",
                        "exitCode": 42,
                    }
                },
            }
        },
    )

    ctx = ReconcileContext(worker_exit_code=99)
    payload = build_failure_payload(record, store=store, reconcile_context=ctx)

    assert payload["exit_code"] == 42


def test_build_failure_payload_reconcile_context_does_not_overwrite_stderr(
    tmp_path,
) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    record = store.create_flow_run(
        run_id="run-ctx-stderr",
        flow_type="ticket_flow",
        input_data={},
    )
    store.create_telemetry(
        telemetry_id="tel-1",
        run_id=record.id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": "true",
                        "stderr": "from-telemetry",
                    }
                },
            }
        },
    )

    ctx = ReconcileContext(worker_stderr_tail="from-worker")
    payload = build_failure_payload(record, store=store, reconcile_context=ctx)

    assert payload["stderr_tail"] == "from-telemetry"


def test_build_failure_payload_no_crash_key_when_crash_info_is_none() -> None:
    record = _build_record(error_message="timeout")
    ctx = ReconcileContext(worker_exit_code=1)
    payload = build_failure_payload(
        record, error_message="timeout", reconcile_context=ctx
    )

    assert "crash" not in payload


def test_ensure_failure_payload_passes_reconcile_context() -> None:
    record = _build_record(error_message="Worker died")
    ctx = ReconcileContext(
        worker_exit_code=137,
        crash_info={"exit_code": 137},
    )
    state = ensure_failure_payload(
        {},
        record=record,
        step_id="step-1",
        error_message="Worker died",
        store=None,
        reconcile_context=ctx,
    )

    failure = state["failure"]
    assert failure["exit_code"] == 137
    assert failure["crash"] == {"exit_code": 137}


def test_ensure_failure_payload_skips_when_failure_already_present() -> None:
    record = _build_record(error_message="first")
    existing_failure = {"failed_at": "2026-01-01T00:00:00Z", "step": "old"}
    state = ensure_failure_payload(
        {"failure": existing_failure},
        record=record,
        step_id="step-2",
        error_message="second",
        store=None,
        reconcile_context=ReconcileContext(worker_exit_code=99),
    )

    assert state["failure"]["step"] == "old"


def test_build_failure_event_data_basic() -> None:
    event_data = build_failure_event_data(
        {},
        error_message="Worker died",
        note="worker-dead",
    )

    assert event_data == {"error": "Worker died", "reason": "worker-dead"}


def test_build_failure_event_data_with_reconcile_context() -> None:
    crash_info = {
        "timestamp": "2026-03-21T00:01:00Z",
        "last_event": "outputDelta",
        "exception": "segfault",
        "exit_code": 139,
        "signal": "SIGSEGV",
    }
    ctx = ReconcileContext(
        crash_info=crash_info,
        last_app_event_method="outputDelta",
        last_turn_id="turn-42",
    )
    event_data = build_failure_event_data(
        {},
        error_message="Worker died",
        note="worker-dead",
        reconcile_context=ctx,
    )

    assert event_data["error"] == "Worker died"
    assert event_data["reason"] == "worker-dead"
    assert event_data["last_app_event_method"] == "outputDelta"
    assert event_data["last_turn_id"] == "turn-42"
    assert event_data["worker_crash"]["timestamp"] == "2026-03-21T00:01:00Z"
    assert event_data["worker_crash"]["exit_code"] == 139
    assert event_data["worker_crash"]["signal"] == "SIGSEGV"


def test_build_failure_event_data_without_reconcile_context() -> None:
    event_data = build_failure_event_data({}, error_message="err", note="step_failed")

    assert "worker_crash" not in event_data
    assert "last_app_event_method" not in event_data
    assert "last_turn_id" not in event_data


def test_runtime_and_reconciler_produce_same_failure_payload() -> None:
    record = _build_record(error_message="Connection error")

    runtime_payload = build_failure_payload(record, error_message="Connection error")
    reconcile_payload = build_failure_payload(
        record, error_message="Connection error", reconcile_context=ReconcileContext()
    )

    shared_keys = (
        "failed_at",
        "ticket_id",
        "step",
        "last_step",
        "retryable",
        "failure_class",
        "failure_reason_code",
        "last_event_seq",
        "last_event_at",
    )
    for key in shared_keys:
        assert (
            runtime_payload[key] == reconcile_payload[key]
        ), f"Mismatch on key '{key}'"

    assert runtime_payload["failure_reason_code"] == "network_error"
    assert runtime_payload["retryable"] is True
