from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from codex_autorunner.core.orchestration.execution_history_diagnostics import (
    CompletionGapDetection,
    ExecutionHistoryDiagnosticReport,
    ExecutionHistoryMetrics,
    ExecutionHistoryThresholdBreach,
    ExecutionHistoryThresholds,
    ExecutionHistoryTopN,
    check_thresholds,
    collect_canonical_turn_state_diagnostics,
    collect_execution_history_metrics,
    collect_top_n_heavy_executions,
    detect_completion_gap_repeated_attempts,
    log_compaction,
    log_dedupe,
    log_quarantine,
    log_retention_prune,
    log_spill_to_cold,
    log_startup_recovery,
    log_truncation,
    log_vacuum,
    run_execution_history_diagnostics,
)
from codex_autorunner.core.orchestration.runtime_chain_diagnostics import (
    RUNTIME_CHAIN_DRIFT,
    RUNTIME_CHAIN_PROJECTED_UNKNOWN,
    build_runtime_chain_diagnostic,
)
from codex_autorunner.core.orchestration.sqlite import (
    initialize_orchestration_sqlite,
    open_orchestration_sqlite,
)
from codex_autorunner.core.runtime_identity import (
    RUNTIME_STAGE_LAUNCH,
    RuntimeIdentityEnvelope,
    RuntimeIdentityStage,
)


def _seed_execution(
    hub_root: Path,
    *,
    execution_id: str,
    status: str = "completed",
    started_at: str = "2026-04-12T00:00:00Z",
    finished_at: str = "2026-04-12T00:05:00Z",
    output_chunks: int = 3,
) -> None:
    initialize_orchestration_sqlite(hub_root, durable=False)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_thread_targets (
                    thread_target_id, agent_id, backend_thread_id, repo_id,
                    resource_kind, resource_id, workspace_root, display_name,
                    lifecycle_status, runtime_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_target_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    backend_thread_id = excluded.backend_thread_id,
                    repo_id = excluded.repo_id,
                    resource_kind = excluded.resource_kind,
                    resource_id = excluded.resource_id,
                    workspace_root = excluded.workspace_root,
                    display_name = excluded.display_name,
                    lifecycle_status = excluded.lifecycle_status,
                    runtime_status = excluded.runtime_status,
                    updated_at = excluded.updated_at
                """,
                (
                    "thread-1",
                    "codex",
                    "backend-thread-1",
                    "repo-1",
                    "repo",
                    "repo-1",
                    str(hub_root / "workspace"),
                    "Primary",
                    "active",
                    status,
                    started_at,
                    finished_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_thread_executions (
                    execution_id, thread_target_id, client_request_id,
                    request_kind, prompt_text, status, backend_turn_id,
                    assistant_text, error_text, model_id, reasoning_level,
                    transcript_mirror_id, started_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    "thread-1",
                    f"client-{execution_id}",
                    "message",
                    "Summarize state",
                    status,
                    "backend-turn-1",
                    "",
                    None,
                    "gpt-test",
                    "high",
                    None,
                    started_at,
                    finished_at,
                    started_at,
                ),
            )

            rows: list[tuple[str, str, str, str, dict[str, object]]] = [
                (
                    f"turn-timeline:{execution_id}:0001",
                    "turn_started",
                    started_at,
                    "recorded",
                    {
                        "event_index": 1,
                        "event_family": "run_notice",
                        "event": {
                            "timestamp": started_at,
                            "kind": "info",
                            "message": "started",
                        },
                    },
                ),
                (
                    f"turn-timeline:{execution_id}:0002",
                    "run_notice",
                    "2026-04-12T00:00:30Z",
                    "recorded",
                    {
                        "event_index": 2,
                        "event_family": "run_notice",
                        "event": {
                            "timestamp": "2026-04-12T00:00:30Z",
                            "kind": "thinking",
                            "message": "planning",
                        },
                    },
                ),
                (
                    f"turn-timeline:{execution_id}:0003",
                    "tool_call",
                    "2026-04-12T00:01:00Z",
                    "recorded",
                    {
                        "event_index": 3,
                        "event_family": "tool_call",
                        "event": {
                            "timestamp": "2026-04-12T00:01:00Z",
                            "tool_name": "shell",
                            "tool_input": {"cmd": "pwd"},
                        },
                    },
                ),
                (
                    f"turn-timeline:{execution_id}:0004",
                    "tool_result",
                    "2026-04-12T00:01:01Z",
                    "completed",
                    {
                        "event_index": 4,
                        "event_family": "tool_result",
                        "event": {
                            "timestamp": "2026-04-12T00:01:01Z",
                            "tool_name": "shell",
                            "status": "completed",
                            "result": {"stdout": "/tmp"},
                        },
                    },
                ),
            ]
            next_index = 5
            for chunk in range(output_chunks):
                timestamp = f"2026-04-12T00:02:{chunk:02d}Z"
                rows.append(
                    (
                        f"turn-timeline:{execution_id}:{next_index:04d}",
                        "output_delta",
                        timestamp,
                        "recorded",
                        {
                            "event_index": next_index,
                            "event_family": "output_delta",
                            "event": {
                                "timestamp": timestamp,
                                "delta_type": "assistant_message",
                                "content": f"chunk-{chunk} ",
                            },
                        },
                    )
                )
                next_index += 1
            rows.extend(
                [
                    (
                        f"turn-timeline:{execution_id}:{next_index:04d}",
                        "token_usage",
                        "2026-04-12T00:04:00Z",
                        "recorded",
                        {
                            "event_index": next_index,
                            "event_family": "token_usage",
                            "event": {
                                "timestamp": "2026-04-12T00:04:00Z",
                                "usage": {"input": 12, "output": 7},
                            },
                        },
                    ),
                    (
                        f"turn-timeline:{execution_id}:{next_index + 1:04d}",
                        "turn_completed" if status != "failed" else "turn_failed",
                        finished_at,
                        "ok" if status != "failed" else "error",
                        {
                            "event_index": next_index + 1,
                            "event_family": "terminal",
                            "event": {
                                "timestamp": finished_at,
                                "final_message": "done",
                                "error_message": "",
                            },
                        },
                    ),
                ]
            )
            for event_id, event_type, timestamp, event_status, payload in rows:
                conn.execute(
                    """
                    INSERT INTO orch_event_projections (
                        event_id, event_family, event_type, target_kind,
                        target_id, execution_id, repo_id, resource_kind,
                        resource_id, run_id, timestamp, status, payload_json,
                        processed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "turn.timeline",
                        event_type,
                        "thread_target",
                        "thread-1",
                        execution_id,
                        "repo-1",
                        "repo",
                        "repo-1",
                        None,
                        timestamp,
                        event_status,
                        json.dumps(payload),
                        1,
                    ),
                )


def _canonical_payloads(
    *,
    execution_id: str,
    status: str,
    agent: str = "codex",
    model: str | None = "gpt-test",
    error_text: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    request: dict[str, object] = {
        "contract_version": 1,
        "request_id": f"request-{execution_id}",
        "target_id": "thread-1",
        "target_kind": "thread",
        "workspace_root": "/workspace",
        "request_kind": "message",
        "busy_policy": "queue",
        "prompt_text": "Summarize state",
        "input_items": [],
        "context_profile": None,
        "agent": agent,
        "profile": None,
        "model": model,
        "model_payload": (
            {"providerID": "zai-coding-plan", "modelID": "glm-5.1"}
            if agent == "opencode"
            else {}
        ),
        "reasoning": "high",
        "approval_policy": "never",
        "approval_mode": None,
        "sandbox_policy": "dangerFullAccess",
        "client_request_id": f"client-{execution_id}",
        "idempotency_key": execution_id,
        "correlation_id": None,
        "origin": {
            "kind": "surface",
            "source_id": "discord:channel-1",
            "surface_kind": "discord",
            "surface_key": "channel-1",
            "automation_rule_id": None,
            "publish_operation_id": None,
            "parent_request_id": None,
            "metadata": {},
        },
        "metadata": {},
        "delivery_intents": [],
    }
    record_status = "completed" if status == "ok" else status
    record: dict[str, object] = {
        "contract_version": 1,
        "request_id": request["request_id"],
        "execution_id": execution_id,
        "status": record_status,
        "queued_at": "2026-04-12T00:00:00Z",
        "claimed_at": None,
        "started_at": "2026-04-12T00:00:00Z",
        "terminal_at": ("2026-04-12T00:05:00Z" if record_status != "running" else None),
        "backend_conversation_id": "backend-thread-1",
        "backend_turn_id": "backend-turn-1",
        "assistant_text": "done" if record_status == "completed" else None,
        "error_text": error_text,
        "transcript_ref": None,
        "timeline_ref": None,
        "cold_trace_ref": None,
        "conflict_evidence": {},
        "metadata": {},
        "request": request,
    }
    return request, record


def _attach_canonical_payloads(
    hub_root: Path,
    *,
    execution_id: str,
    status: str,
    agent: str = "codex",
    model: str | None = "gpt-test",
    error_text: str | None = None,
) -> None:
    request, record = _canonical_payloads(
        execution_id=execution_id,
        status=status,
        agent=agent,
        model=model,
        error_text=error_text,
    )
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET turn_contract_version = 1,
                       turn_request_json = ?,
                       turn_record_json = ?,
                       error_text = COALESCE(?, error_text)
                 WHERE execution_id = ?
                """,
                (
                    json.dumps(request),
                    json.dumps(record),
                    error_text,
                    execution_id,
                ),
            )


def test_collect_metrics_counts_executions_and_rows(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-1", output_chunks=5)

    metrics = collect_execution_history_metrics(hub_root)

    assert metrics.total_executions == 1
    assert metrics.terminal_executions == 1
    assert metrics.timeline_rows > 0
    assert metrics.checkpoints == 0
    assert metrics.event_count_by_execution.get("exec-1", 0) > 0
    assert "output_delta" in metrics.hot_row_count_by_family
    assert "tool_call" in metrics.hot_row_count_by_family


def test_collect_metrics_multiple_executions(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-a", output_chunks=2)
    _seed_execution(hub_root, execution_id="exec-b", output_chunks=4)

    metrics = collect_execution_history_metrics(hub_root)

    assert metrics.total_executions == 2
    assert metrics.terminal_executions == 2
    assert metrics.event_count_by_execution.get("exec-a", 0) > 0
    assert metrics.event_count_by_execution.get("exec-b", 0) > 0


def test_collect_metrics_uses_event_type_when_payload_json_is_malformed(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-corrupt", output_chunks=2)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_event_projections
                   SET payload_json = 'not-json'
                 WHERE execution_id = ?
                   AND event_type IN ('tool_call', 'output_delta')
                """,
                ("exec-corrupt",),
            )

    metrics = collect_execution_history_metrics(hub_root)

    assert metrics.event_count_by_execution.get("exec-corrupt", 0) > 0
    assert metrics.hot_row_count_by_family.get("tool_call", 0) == 1
    assert metrics.hot_row_count_by_family.get("output_delta", 0) == 2


def test_collect_top_n_heavy_executions(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-small", output_chunks=1)
    _seed_execution(hub_root, execution_id="exec-large", output_chunks=10)

    top_n = collect_top_n_heavy_executions(hub_root, top_n=5)

    assert len(top_n.top_heavy_executions) <= 5
    if len(top_n.top_heavy_executions) >= 2:
        assert (
            top_n.top_heavy_executions[0]["hot_rows"]
            >= top_n.top_heavy_executions[1]["hot_rows"]
        )
    assert any(e["execution_id"] == "exec-large" for e in top_n.top_heavy_executions)
    assert len(top_n.top_event_families) > 0


def test_collect_top_n_uses_grouped_event_types_without_payload_json(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-topn", output_chunks=3)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_event_projections
                   SET payload_json = ''
                 WHERE execution_id = ?
                   AND event_type IN ('turn_started', 'run_notice', 'tool_call')
                """,
                ("exec-topn",),
            )

    top_n = collect_top_n_heavy_executions(hub_root, top_n=5)
    family_rows = {
        entry["event_family"]: entry["hot_rows"] for entry in top_n.top_event_families
    }

    assert family_rows.get("run_notice", 0) >= 2
    assert family_rows.get("tool_call", 0) == 1


def test_check_thresholds_no_breach() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=10,
        terminal_executions=5,
        timeline_rows=100,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=1024,
        trace_file_count=5,
        hot_row_count_by_family={"tool_call": 10, "output_delta": 20},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={"exec-1": 10},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    assert len(breaches) == 0


def test_check_thresholds_cold_trace_bytes_warning() -> None:
    warning_bytes = 50 * 1024 * 1024
    metrics = ExecutionHistoryMetrics(
        total_executions=10,
        terminal_executions=5,
        timeline_rows=100,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=warning_bytes,
        trace_file_count=5,
        hot_row_count_by_family={},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    warning_breaches = [b for b in breaches if b.level == "warning"]
    assert any(b.metric == "total_trace_bytes" for b in warning_breaches)


def test_check_thresholds_cold_trace_bytes_error() -> None:
    error_bytes = 200 * 1024 * 1024
    metrics = ExecutionHistoryMetrics(
        total_executions=10,
        terminal_executions=5,
        timeline_rows=100,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=error_bytes,
        trace_file_count=5,
        hot_row_count_by_family={},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    error_breaches = [b for b in breaches if b.level == "error"]
    assert any(b.metric == "total_trace_bytes" for b in error_breaches)


def test_check_thresholds_hot_row_count_warning() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=10,
        terminal_executions=5,
        timeline_rows=5000,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=0,
        trace_file_count=0,
        hot_row_count_by_family={"run_notice": 100},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    assert any(
        b.metric == "hot_timeline_rows" and b.level == "warning" for b in breaches
    )


def test_check_thresholds_hot_row_count_error() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=10,
        terminal_executions=5,
        timeline_rows=20000,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=0,
        trace_file_count=0,
        hot_row_count_by_family={"run_notice": 100},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    assert any(b.metric == "hot_timeline_rows" and b.level == "error" for b in breaches)


def test_check_thresholds_notice_amplification_warning() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=1,
        terminal_executions=1,
        timeline_rows=100,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=0,
        trace_file_count=0,
        hot_row_count_by_family={"run_notice": 50},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    assert any(
        b.metric == "notice_amplification" and b.level == "warning" for b in breaches
    )


def test_check_thresholds_notice_amplification_error() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=1,
        terminal_executions=1,
        timeline_rows=250,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=0,
        trace_file_count=0,
        hot_row_count_by_family={"run_notice": 200},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    breaches = check_thresholds(metrics)

    assert any(
        b.metric == "notice_amplification" and b.level == "error" for b in breaches
    )


def test_check_thresholds_oversized_execution() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=10,
        terminal_executions=5,
        timeline_rows=200,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=0,
        trace_file_count=0,
        hot_row_count_by_family={},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={"exec-big": 128},
        oversized_execution_ids=("exec-big",),
    )

    breaches = check_thresholds(metrics)

    assert any(
        b.metric == "oversized_execution" and b.level == "warning" for b in breaches
    )


def test_check_thresholds_custom_thresholds() -> None:
    metrics = ExecutionHistoryMetrics(
        total_executions=1,
        terminal_executions=1,
        timeline_rows=100,
        checkpoints=5,
        finalized_manifests=5,
        archived_manifests=0,
        total_trace_bytes=0,
        trace_file_count=0,
        hot_row_count_by_family={"run_notice": 10},
        cold_trace_bytes_by_execution={},
        event_count_by_execution={},
        oversized_execution_ids=(),
    )

    custom = ExecutionHistoryThresholds(notice_amplification_warning=5)
    breaches = check_thresholds(metrics, thresholds=custom)

    assert any(
        b.metric == "notice_amplification" and b.level == "warning" for b in breaches
    )


def test_detect_completion_gap_repeated_attempts(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    initialize_orchestration_sqlite(hub_root, durable=False)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        for i in range(5):
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id, event_family, event_type, target_kind,
                    target_id, execution_id, repo_id, resource_kind,
                    resource_id, run_id, timestamp, status, payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"turn-timeline:exec-gap:{i + 1:04d}",
                    "turn.timeline",
                    "run_notice",
                    "thread_target",
                    "thread-1",
                    "exec-gap",
                    "repo-1",
                    "repo",
                    "repo-1",
                    None,
                    f"2026-04-12T00:0{i}:00Z",
                    "recorded",
                    json.dumps(
                        {
                            "event_family": "run_notice",
                            "event": {
                                "kind": "completion_gap",
                                "message": f"attempt {i}",
                            },
                        }
                    ),
                    1,
                ),
            )

    thresholds = ExecutionHistoryThresholds(
        completion_gap_attempts_warning=3,
        completion_gap_attempts_error=10,
    )
    detections = detect_completion_gap_repeated_attempts(
        hub_root, thresholds=thresholds
    )

    assert len(detections) == 1
    assert detections[0].execution_id == "exec-gap"
    assert detections[0].attempt_count == 5
    assert detections[0].breach_level == "warning"


def test_detect_completion_gap_error_threshold(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    initialize_orchestration_sqlite(hub_root, durable=False)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        for i in range(12):
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id, event_family, event_type, target_kind,
                    target_id, execution_id, repo_id, resource_kind,
                    resource_id, run_id, timestamp, status, payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"turn-timeline:exec-gap2:{i + 1:04d}",
                    "turn.timeline",
                    "run_notice",
                    "thread_target",
                    "thread-1",
                    "exec-gap2",
                    "repo-1",
                    "repo",
                    "repo-1",
                    None,
                    f"2026-04-12T00:{i:02d}:00Z",
                    "recorded",
                    json.dumps(
                        {
                            "event_family": "run_notice",
                            "event": {
                                "kind": "completion_gap",
                                "message": f"attempt {i}",
                            },
                        }
                    ),
                    1,
                ),
            )

    thresholds = ExecutionHistoryThresholds(
        completion_gap_attempts_warning=3,
        completion_gap_attempts_error=10,
    )
    detections = detect_completion_gap_repeated_attempts(
        hub_root, thresholds=thresholds
    )

    assert len(detections) == 1
    assert detections[0].breach_level == "error"


def test_detect_completion_gap_no_false_positives(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    initialize_orchestration_sqlite(hub_root, durable=False)

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        conn.execute(
            """
            INSERT INTO orch_event_projections (
                event_id, event_family, event_type, target_kind,
                target_id, execution_id, repo_id, resource_kind,
                resource_id, run_id, timestamp, status, payload_json,
                processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn-timeline:exec-ok:0001",
                "turn.timeline",
                "run_notice",
                "thread_target",
                "thread-1",
                "exec-ok",
                "repo-1",
                "repo",
                "repo-1",
                None,
                "2026-04-12T00:00:00Z",
                "recorded",
                json.dumps(
                    {
                        "event_family": "run_notice",
                        "event": {"kind": "progress", "message": "working"},
                    }
                ),
                1,
            ),
        )

    detections = detect_completion_gap_repeated_attempts(hub_root)

    assert len(detections) == 0


def test_canonical_turn_diagnostics_classify_stale_running(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(
        hub_root,
        execution_id="exec-stale",
        status="running",
    )
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_executions
                   SET finished_at = NULL
                 WHERE execution_id = ?
                """,
                ("exec-stale",),
            )
    _attach_canonical_payloads(hub_root, execution_id="exec-stale", status="running")

    diagnostics = collect_canonical_turn_state_diagnostics(
        hub_root,
        stale_after_seconds=60,
    )

    assert len(diagnostics) == 1
    diag = diagnostics[0]
    assert diag.request_id == "request-exec-stale"
    assert diag.execution_id == "exec-stale"
    assert diag.surface_origin == "discord:channel-1"
    assert diag.target["target_id"] == "thread-1"
    assert diag.lifecycle_phase == "stale_running"
    assert diag.recovery_action == "classify_stale_running"
    assert diag.runtime_options["model"] == "gpt-test"


def test_canonical_turn_diagnostics_classify_queued_replay(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-queued", status="queued")
    _attach_canonical_payloads(hub_root, execution_id="exec-queued", status="queued")

    diagnostics = collect_canonical_turn_state_diagnostics(hub_root)

    assert diagnostics[0].lifecycle_phase == "queued"
    assert diagnostics[0].recovery_action == "replay_queued"


def test_canonical_turn_diagnostics_detect_duplicate_terminal_writes(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-dup", output_chunks=1)
    _attach_canonical_payloads(hub_root, execution_id="exec-dup", status="completed")
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id, event_family, event_type, target_kind,
                    target_id, execution_id, repo_id, resource_kind,
                    resource_id, run_id, timestamp, status, payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "turn-timeline:exec-dup:9999",
                    "turn.timeline",
                    "turn_completed",
                    "thread_target",
                    "thread-1",
                    "exec-dup",
                    "repo-1",
                    "repo",
                    "repo-1",
                    None,
                    "2026-04-12T00:05:01Z",
                    "ok",
                    json.dumps(
                        {
                            "event": {
                                "final_message": "done",
                                "error_message": "",
                            }
                        }
                    ),
                    1,
                ),
            )

    diag = collect_canonical_turn_state_diagnostics(hub_root)[0]

    assert diag.terminal_status == "completed"
    assert diag.evidence["duplicate_terminal_writes"] is True
    assert diag.evidence["conflicting_terminal_writes"] is False


def test_canonical_turn_diagnostics_detect_conflicting_terminal_writes(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-conflict", output_chunks=1)
    _attach_canonical_payloads(
        hub_root, execution_id="exec-conflict", status="completed"
    )
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id, event_family, event_type, target_kind,
                    target_id, execution_id, repo_id, resource_kind,
                    resource_id, run_id, timestamp, status, payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "turn-timeline:exec-conflict:9999",
                    "turn.timeline",
                    "turn_failed",
                    "thread_target",
                    "thread-1",
                    "exec-conflict",
                    "repo-1",
                    "repo",
                    "repo-1",
                    None,
                    "2026-04-12T00:05:01Z",
                    "error",
                    json.dumps({"event": {"error_message": "late failure"}}),
                    1,
                ),
            )

    diag = collect_canonical_turn_state_diagnostics(hub_root)[0]

    assert diag.evidence["duplicate_terminal_writes"] is True
    assert diag.evidence["conflicting_terminal_writes"] is True


def test_canonical_turn_diagnostics_preserve_opencode_first_event_timeout(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-opencode", status="failed")
    _attach_canonical_payloads(
        hub_root,
        execution_id="exec-opencode",
        status="failed",
        agent="opencode",
        model="zai-coding-plan/glm-5.1",
        error_text="opencode_first_event_timeout: no first event",
    )

    diag = collect_canonical_turn_state_diagnostics(hub_root)[0]

    assert diag.runtime_options["agent"] == "opencode"
    assert diag.runtime_options["model"] == "zai-coding-plan/glm-5.1"
    assert diag.terminal_status == "failed"
    assert diag.evidence["runtime_error_code"] == "opencode_first_event_timeout"
    assert "opencode_first_event_timeout" in diag.evidence["error_text"]


def test_run_diagnostics_full_report(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-1", output_chunks=5)

    report = run_execution_history_diagnostics(hub_root)

    assert isinstance(report, ExecutionHistoryDiagnosticReport)
    assert report.metrics.total_executions == 1
    assert isinstance(report.top_n, ExecutionHistoryTopN)
    assert isinstance(report.threshold_breaches, tuple)
    assert report.generated_at


def test_runtime_chain_diagnostic_reports_automation_edge_drift(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        conn.execute(
            """
            INSERT INTO orch_automation_rules (
                rule_id, name, trigger_kind, target_policy, executor_kind,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rule-runtime",
                "Runtime rule",
                "manual",
                "default",
                "agent_task_turn",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO orch_automation_events (
                event_id, event_type, observed_at
            ) VALUES (?, ?, ?)
            """,
            (
                "event-runtime",
                "manual.run",
                "2026-05-11T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO orch_automation_jobs (
                job_id, rule_id, event_id, state, dedupe_key, available_at,
                updated_at, created_at, target_json, executor_json, policy_json,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-runtime",
                "rule-runtime",
                "event-runtime",
                "running",
                "dedupe-runtime",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:00Z",
                "{}",
                json.dumps(
                    {
                        "kind": "agent_task_turn",
                        "requested_runtime": {
                            "agent": "opencode",
                            "model": "zai-coding-plan/glm-5.1",
                        },
                    }
                ),
                "{}",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO orch_automation_child_execution_edges (
                edge_id, parent_job_id, child_kind, child_id,
                requested_runtime_json, actual_runtime_json,
                terminal_mapping_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "edge-runtime",
                "job-runtime",
                "agent_task",
                "turn-runtime",
                json.dumps({"agent": "opencode", "model": "zai-coding-plan/glm-5.1"}),
                json.dumps({"agent": "opencode", "model": "glm-5v-turbo"}),
                "{}",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:00Z",
            ),
        )

    report = build_runtime_chain_diagnostic(
        hub_root,
        automation_child_edge_id="edge-runtime",
        durable=False,
    )

    assert report.row_identity["automation_job_id"] == "job-runtime"
    assert report.stages["requested"]["canonical_model_label"] == (
        "zai-coding-plan/glm-5.1"
    )
    assert report.stages["effective"]["canonical_model_label"] == "glm-5v-turbo"
    assert RUNTIME_CHAIN_DRIFT in {finding.code for finding in report.findings}


def test_execution_history_diagnostics_include_runtime_chain_invariants(
    tmp_path: Path, monkeypatch: Any
) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    runtime_identity = RuntimeIdentityEnvelope(
        launch=RuntimeIdentityStage(
            stage=RUNTIME_STAGE_LAUNCH,
            logical_agent="opencode",
            canonical_model_label="zai-coding-plan/glm-5.1",
            source="managed_thread_launch",
        )
    )
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id, agent_id, display_name, lifecycle_status,
                runtime_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "missing-thread",
                "opencode",
                "Missing projection",
                "active",
                "running",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO orch_thread_executions (
                execution_id, thread_target_id, request_kind, status,
                runtime_identity_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "turn-without-projection",
                "missing-thread",
                "message",
                "running",
                runtime_identity.to_json(),
                "2026-05-11T00:00:00Z",
            ),
        )
    monkeypatch.setattr(
        "codex_autorunner.core.orchestration.runtime_chain_diagnostics._projected_chat_row",
        lambda *args, **kwargs: None,
    )

    report = run_execution_history_diagnostics(hub_root)

    assert report.runtime_chains
    assert RUNTIME_CHAIN_PROJECTED_UNKNOWN in {
        finding.code for chain in report.runtime_chains for finding in chain.findings
    }
    assert any(
        breach.metric == "runtime_chain_invariant"
        for breach in report.threshold_breaches
    )


def test_run_diagnostics_with_custom_thresholds(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-1", output_chunks=5)

    thresholds = ExecutionHistoryThresholds(
        hot_row_count_warning=2,
        top_n_heavy_executions=3,
    )
    report = run_execution_history_diagnostics(hub_root, thresholds=thresholds)

    assert report.metrics.total_executions == 1
    hot_breaches = [
        b for b in report.threshold_breaches if b.metric == "hot_timeline_rows"
    ]
    assert len(hot_breaches) > 0


def test_threshold_breach_to_dict() -> None:
    breach = ExecutionHistoryThresholdBreach(
        level="warning",
        metric="test_metric",
        value=100,
        threshold=50,
        message="test breach",
        context={"key": "value"},
    )

    d = breach.to_dict()
    assert d["level"] == "warning"
    assert d["metric"] == "test_metric"
    assert d["value"] == 100
    assert d["threshold"] == 50
    assert d["context"]["key"] == "value"


def test_completion_gap_detection_to_dict() -> None:
    detection = CompletionGapDetection(
        execution_id="exec-1",
        attempt_count=5,
        first_attempt_at="2026-04-12T00:00:00Z",
        last_attempt_at="2026-04-12T00:05:00Z",
        breach_level="warning",
        context={"attempts": 5},
    )

    d = detection.to_dict()
    assert d["execution_id"] == "exec-1"
    assert d["attempt_count"] == 5
    assert d["breach_level"] == "warning"


def test_structured_log_helpers_emit_valid_json(tmp_path: Path, caplog: Any) -> None:
    with caplog.at_level(
        logging.DEBUG, logger="codex_autorunner.execution_history_diagnostics"
    ):
        log_spill_to_cold(
            execution_id="exec-1",
            event_family="tool_call",
            has_cold_trace=True,
            hot_rows_so_far=129,
            hot_limit=128,
        )
        log_dedupe(
            execution_id="exec-1",
            event_family="run_notice",
            dedupe_reason="duplicate_notice",
            deduped_count=3,
        )
        log_truncation(
            execution_id="exec-1",
            event_family="tool_call",
            original_chars=5000,
            truncated_chars=2048,
            contract="structured_event",
        )
        log_compaction(
            execution_id="exec-1",
            rows_before=100,
            rows_after=16,
            rows_deleted=84,
            cold_trace_preserved=True,
        )
        log_retention_prune(
            pruned_execution_ids=5,
            pruned_trace_ids=3,
            hot_rows_deleted=200,
            bytes_reclaimed=1024,
        )
        log_vacuum(
            database_path="/tmp/test.db",
            size_before=1000000,
            size_after=500000,
            reclaimed_bytes=500000,
        )
        log_quarantine(
            execution_id="exec-1",
            reason="corrupt_trace",
            context={"artifact_missing": True},
        )
        log_startup_recovery(
            duration_seconds=2.5,
            executions_recovered=10,
            checkpoints_loaded=5,
        )

    messages = caplog.messages
    assert len(messages) >= 8

    for msg in messages:
        parsed = json.loads(msg)
        assert "event" in parsed
        assert isinstance(parsed["event"], str)


def test_top_n_ordered_by_size(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-small", output_chunks=1)
    _seed_execution(hub_root, execution_id="exec-medium", output_chunks=5)
    _seed_execution(hub_root, execution_id="exec-large", output_chunks=15)

    top_n = collect_top_n_heavy_executions(hub_root, top_n=3)

    hot_rows = [e["hot_rows"] for e in top_n.top_heavy_executions]
    assert hot_rows == sorted(hot_rows, reverse=True)


def test_diagnostic_report_to_dict(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-1", output_chunks=2)

    report = run_execution_history_diagnostics(hub_root)
    d = report.to_dict()

    assert "metrics" in d
    assert "top_n" in d
    assert "threshold_breaches" in d
    assert "generated_at" in d
    assert isinstance(d["metrics"]["total_executions"], int)
    assert isinstance(d["threshold_breaches"], (list, tuple))
