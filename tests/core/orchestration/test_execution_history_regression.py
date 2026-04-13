"""Performance, scale, and regression tests for execution-history persistence
and recovery.

These tests prevent recurrence of the amplification pattern where cumulative
reasoning/progress updates produced unbounded hot persistence growth, and they
assert bounded startup/recovery cost against realistic history sizes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceStore
from codex_autorunner.core.orchestration.execution_history import (
    build_hot_projection_envelope,
    truncate_hot_event_payload,
)
from codex_autorunner.core.orchestration.execution_history_diagnostics import (
    collect_execution_history_metrics,
    run_execution_history_diagnostics,
)
from codex_autorunner.core.orchestration.execution_history_maintenance import (
    ExecutionHistoryMaintenancePolicy,
    backfill_legacy_execution_history,
    compact_completed_execution_history,
    prune_execution_history_retention,
    vacuum_execution_history,
)
from codex_autorunner.core.orchestration.sqlite import (
    initialize_orchestration_sqlite,
    open_orchestration_sqlite,
)
from codex_autorunner.core.orchestration.turn_timeline import (
    list_turn_timeline,
    persist_turn_timeline,
)
from codex_autorunner.core.ports.run_event import (
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
)


def _seed_thread_and_execution(
    hub_root: Path,
    *,
    execution_id: str,
    thread_target_id: str = "thread-perf-1",
    status: str = "completed",
    started_at: str = "2026-04-12T00:00:00Z",
    finished_at: str = "2026-04-12T00:05:00Z",
    assistant_text: str = "",
    error_text: str | None = None,
    repo_id: str = "repo-perf",
) -> None:
    initialize_orchestration_sqlite(hub_root, durable=False)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_thread_targets (
                    thread_target_id, agent_id, backend_thread_id,
                    repo_id, resource_kind, resource_id,
                    workspace_root, display_name,
                    lifecycle_status, runtime_status,
                    created_at, updated_at
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
                    thread_target_id,
                    "codex",
                    "backend-thread-perf",
                    repo_id,
                    "repo",
                    repo_id,
                    str(hub_root / "workspace"),
                    "PerfTest",
                    "active",
                    status,
                    started_at,
                    finished_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_thread_executions (
                    execution_id, thread_target_id,
                    client_request_id, request_kind, prompt_text,
                    status, backend_turn_id,
                    assistant_text, error_text,
                    model_id, reasoning_level, transcript_mirror_id,
                    started_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    thread_target_id,
                    f"client-{execution_id}",
                    "message",
                    "Perf test prompt",
                    status,
                    "backend-turn-perf",
                    assistant_text,
                    error_text,
                    "gpt-perf",
                    "high",
                    None,
                    started_at,
                    finished_at,
                    started_at,
                ),
            )


def _seed_legacy_timeline_rows(
    hub_root: Path,
    *,
    execution_id: str,
    thread_target_id: str = "thread-perf-1",
    repo_id: str = "repo-perf",
    rows: list[dict[str, Any]],
) -> None:
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            for idx, row in enumerate(rows):
                event_id = row.get(
                    "event_id",
                    f"turn-timeline:{execution_id}:{idx + 1:04d}",
                )
                conn.execute(
                    """
                    INSERT INTO orch_event_projections (
                        event_id, event_family, event_type,
                        target_kind, target_id, execution_id,
                        repo_id, resource_kind, resource_id, run_id,
                        timestamp, status, payload_json, processed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "turn.timeline",
                        row["event_type"],
                        "thread_target",
                        thread_target_id,
                        execution_id,
                        repo_id,
                        "repo",
                        repo_id,
                        None,
                        row["timestamp"],
                        row.get("status", "recorded"),
                        json.dumps(row["payload"]),
                        1,
                    ),
                )


def _build_legacy_timeline_rows(
    *,
    execution_id: str,
    started_at: str = "2026-04-12T00:00:00Z",
    finished_at: str = "2026-04-12T00:05:00Z",
    num_output_deltas: int = 50,
    num_run_notices: int = 20,
    num_tool_calls: int = 5,
    output_content_size: int = 100,
    notice_message_size: int = 80,
    tool_input_size: int = 200,
    tool_result_size: int = 200,
    status: str = "completed",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    idx = 1

    rows.append(
        {
            "event_type": "turn_started",
            "timestamp": started_at,
            "status": "recorded",
            "payload": {
                "event_index": idx,
                "event_family": "run_notice",
                "event": {
                    "timestamp": started_at,
                    "kind": "info",
                    "message": "started",
                },
            },
        }
    )
    idx += 1

    for i in range(num_run_notices):
        ts = f"2026-04-12T00:00:{min(i, 59):02d}Z"
        msg = "n" * notice_message_size
        rows.append(
            {
                "event_type": "run_notice",
                "timestamp": ts,
                "status": "recorded",
                "payload": {
                    "event_index": idx,
                    "event_family": "run_notice",
                    "event": {
                        "timestamp": ts,
                        "kind": "thinking",
                        "message": f"reasoning step {i}: {msg}",
                    },
                },
            }
        )
        idx += 1

    for i in range(num_tool_calls):
        ts = f"2026-04-12T00:01:{min(i * 2, 59):02d}Z"
        tool_input_val = "x" * tool_input_size
        rows.append(
            {
                "event_type": "tool_call",
                "timestamp": ts,
                "status": "recorded",
                "payload": {
                    "event_index": idx,
                    "event_family": "tool_call",
                    "event": {
                        "timestamp": ts,
                        "tool_name": f"tool-{i}",
                        "tool_input": {"data": tool_input_val},
                    },
                },
            }
        )
        idx += 1
        result_ts = f"2026-04-12T00:01:{min(i * 2 + 1, 59):02d}Z"
        result_val = "y" * tool_result_size
        rows.append(
            {
                "event_type": "tool_result",
                "timestamp": result_ts,
                "status": "completed",
                "payload": {
                    "event_index": idx,
                    "event_family": "tool_result",
                    "event": {
                        "timestamp": result_ts,
                        "tool_name": f"tool-{i}",
                        "status": "completed",
                        "result": {"output": result_val},
                    },
                },
            }
        )
        idx += 1

    for i in range(num_output_deltas):
        ts = f"2026-04-12T00:02:{min(i % 60, 59):02d}Z"
        content = "d" * output_content_size
        rows.append(
            {
                "event_type": "output_delta",
                "timestamp": ts,
                "status": "recorded",
                "payload": {
                    "event_index": idx,
                    "event_family": "output_delta",
                    "event": {
                        "timestamp": ts,
                        "delta_type": "assistant_message",
                        "content": content,
                    },
                },
            }
        )
        idx += 1

    rows.append(
        {
            "event_type": "token_usage",
            "timestamp": "2026-04-12T00:04:00Z",
            "status": "recorded",
            "payload": {
                "event_index": idx,
                "event_family": "token_usage",
                "event": {
                    "timestamp": "2026-04-12T00:04:00Z",
                    "usage": {"input": 1000, "output": 500},
                },
            },
        }
    )
    idx += 1

    if status == "failed":
        rows.append(
            {
                "event_type": "turn_failed",
                "timestamp": finished_at,
                "status": "error",
                "payload": {
                    "event_index": idx,
                    "event_family": "terminal",
                    "event": {
                        "timestamp": finished_at,
                        "error_message": "simulated failure",
                    },
                },
            }
        )
    else:
        rows.append(
            {
                "event_type": "turn_completed",
                "timestamp": finished_at,
                "status": "ok",
                "payload": {
                    "event_index": idx,
                    "event_family": "terminal",
                    "event": {
                        "timestamp": finished_at,
                        "final_message": "done",
                    },
                },
            }
        )
    return rows


class TestHotProjectionBoundedGrowth:
    """Regression tests proving that repeated reasoning/progress updates do not
    produce unbounded hot persistence growth.

    The amplification pattern these tests prevent: a runtime that writes
    cumulative thinking/progress text into every hot event row, causing
    row-size growth proportional to N^2 for N events.
    """

    def test_many_output_deltas_hot_rows_stay_within_family_limit(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-many-deltas")

        events = [
            OutputDelta(
                timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                content=f"delta chunk {i} with some text content",
                delta_type="assistant_stream",
            )
            for i in range(300)
        ]

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-many-deltas",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        assert count == 300

        timeline = list_turn_timeline(hub_root, execution_id="exec-many-deltas")
        output_rows = [e for e in timeline if e["event_type"] == "output_delta"]
        assert len(output_rows) == 200

        for row in output_rows:
            event = row.get("event", {})
            content = event.get("content", "")
            assert len(content) <= 512

    def test_many_notices_hot_rows_stay_within_family_limit(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-many-notices")

        events = [
            RunNotice(
                timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                kind="progress",
                message=f"progress update {i}: doing work",
            )
            for i in range(200)
        ]

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-many-notices",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        assert count == 200

        timeline = list_turn_timeline(hub_root, execution_id="exec-many-notices")
        notice_rows = [e for e in timeline if e["event_type"] == "run_notice"]
        assert len(notice_rows) == 100

        for row in notice_rows:
            event = row.get("event", {})
            message = event.get("message", "")
            assert len(message) <= 512

    def test_many_tool_calls_hot_rows_stay_within_family_limit(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-many-tools")

        events: list[Any] = []
        for i in range(150):
            events.append(
                ToolCall(
                    timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    tool_name=f"tool-{i}",
                    tool_input={"cmd": f"command-{i}"},
                )
            )

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-many-tools",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        assert count == 150

        timeline = list_turn_timeline(hub_root, execution_id="exec-many-tools")
        tool_rows = [e for e in timeline if e["event_type"] == "tool_call"]
        assert len(tool_rows) == 128

    def test_many_tool_results_hot_rows_stay_within_family_limit(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-many-results")

        events: list[Any] = []
        for i in range(150):
            events.append(
                ToolResult(
                    timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    tool_name=f"tool-{i}",
                    status="completed",
                    result={"stdout": f"output-{i}"},
                )
            )

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-many-results",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        assert count == 150

        timeline = list_turn_timeline(hub_root, execution_id="exec-many-results")
        result_rows = [e for e in timeline if e["event_type"] == "tool_result"]
        assert len(result_rows) == 128

    def test_many_token_usage_hot_rows_stay_within_family_limit(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-many-tokens")

        events = [
            TokenUsage(
                timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                usage={"input": 100 + i, "output": 50 + i},
            )
            for i in range(100)
        ]

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-many-tokens",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        assert count == 100

        timeline = list_turn_timeline(hub_root, execution_id="exec-many-tokens")
        token_rows = [e for e in timeline if e["event_type"] == "token_usage"]
        assert len(token_rows) == 64

    def test_cumulative_reasoning_does_not_amplify_hot_row_sizes(
        self, tmp_path: Path
    ) -> None:
        """Core regression test: the original amplification pattern.

        Before the fix, each reasoning update wrote the full cumulative text
        into a hot row. With 100 updates of 50 chars each, the last row would
        contain ~5000 chars. After the fix, each row contains only the delta.
        """
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(
            hub_root, execution_id="exec-reasoning-amplification"
        )

        cumulative_text = ""
        events: list[Any] = []
        for i in range(100):
            chunk = f"reasoning step {i} with some detail. "
            cumulative_text += chunk
            events.append(
                RunNotice(
                    timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    kind="thinking",
                    message=cumulative_text,
                )
            )

        persist_turn_timeline(
            hub_root,
            execution_id="exec-reasoning-amplification",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        timeline = list_turn_timeline(
            hub_root, execution_id="exec-reasoning-amplification"
        )
        notice_rows = [e for e in timeline if e["event_type"] == "run_notice"]

        for row in notice_rows:
            event = row.get("event", {})
            message = event.get("message", "")
            assert len(message) <= 512 + 50, (
                f"Hot row has message of {len(message)} chars, "
                f"expected coalesced delta or truncated value"
            )

        checkpoint = ColdTraceStore(hub_root).load_checkpoint(
            "exec-reasoning-amplification"
        )
        assert checkpoint is not None
        assert len(checkpoint.progress_text_preview) <= 240

    def test_checkpoint_size_is_bounded_regardless_of_event_count(
        self, tmp_path: Path
    ) -> None:
        """Checkpoint JSON must stay small even with many events."""
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(
            hub_root, execution_id="exec-checkpoint-size", status="running"
        )

        events: list[Any] = []
        for i in range(500):
            events.append(
                RunNotice(
                    timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    kind="thinking",
                    message=f"thinking about step {i} with some reasoning text that is moderately long",
                )
            )
        for i in range(300):
            events.append(
                OutputDelta(
                    timestamp=f"2026-04-12T01:{i // 60:02d}:{i % 60:02d}Z",
                    content=f"output delta {i} " * 10,
                    delta_type="assistant_stream",
                )
            )
        for i in range(50):
            events.append(
                ToolCall(
                    timestamp=f"2026-04-12T02:{i // 60:02d}:{i % 60:02d}Z",
                    tool_name=f"tool-{i}",
                    tool_input={"cmd": f"run command {i}"},
                )
            )
        events.append(
            TokenUsage(
                timestamp="2026-04-12T03:00:00Z",
                usage={"input": 50000, "output": 25000},
            )
        )

        persist_turn_timeline(
            hub_root,
            execution_id="exec-checkpoint-size",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        store = ColdTraceStore(hub_root)
        checkpoint = store.load_checkpoint("exec-checkpoint-size")
        assert checkpoint is not None

        checkpoint_json = json.dumps(checkpoint.to_dict())
        assert len(checkpoint_json) < 4096, (
            f"Checkpoint JSON is {len(checkpoint_json)} bytes, "
            f"expected under 4KB for bounded snapshot"
        )

        assert len(checkpoint.assistant_text_preview) <= 240
        assert len(checkpoint.progress_text_preview) <= 240
        assert checkpoint.projection_event_cursor == 851

    def test_hot_projection_envelope_payload_is_bounded_for_large_events(
        self,
    ) -> None:
        large_tool_input = {"data": "x" * 100_000}
        event = ToolCall(
            timestamp="2026-04-12T00:00:00Z",
            tool_name="shell",
            tool_input=large_tool_input,
        )
        payload = build_hot_projection_envelope(
            event_index=1,
            event_type="tool_call",
            event=event,
        ).to_payload()

        assert "tool_input" not in payload["event"]
        assert "tool_input_preview" in payload["event"]
        assert len(payload["event"]["tool_input_preview"]) <= 2048
        assert payload["event"]["details_in_cold_trace"] is True

    def test_hot_projection_envelope_truncates_large_tool_result(self) -> None:
        large_result = {"stdout": "y" * 100_000}
        event = ToolResult(
            timestamp="2026-04-12T00:00:00Z",
            tool_name="shell",
            status="completed",
            result=large_result,
        )
        payload = build_hot_projection_envelope(
            event_index=1,
            event_type="tool_result",
            event=event,
        ).to_payload()

        assert "result" not in payload["event"]
        assert "result_preview" in payload["event"]
        assert len(payload["event"]["result_preview"]) <= 2048

    def test_truncate_hot_event_large_notice_data_is_bounded(self) -> None:
        event = RunNotice(
            timestamp="2026-04-12T00:00:00Z",
            kind="progress",
            message="working",
            data={f"key_{i}": f"val_{i}" * 100 for i in range(50)},
        )
        bounded = truncate_hot_event_payload(event, "structured_event")

        assert "data" not in bounded
        assert "data_preview" in bounded
        assert len(bounded["data_preview"]) <= 1024

    def test_spilled_events_tracked_in_checkpoint(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-spilled")

        events = [
            RunNotice(
                timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                kind="progress",
                message=f"notice {i}",
            )
            for i in range(150)
        ]
        persist_turn_timeline(
            hub_root,
            execution_id="exec-spilled",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        checkpoint = ColdTraceStore(hub_root).load_checkpoint("exec-spilled")
        assert checkpoint is not None
        hot_state = checkpoint.hot_projection_state
        assert hot_state is not None
        assert hot_state["family_hot_rows"]["run_notice"] == 100
        assert hot_state["spilled_counts"]["run_notice"] == 50


class TestCompactionAndRetentionAtScale:
    """Compaction, retention, and vacuum must remain efficient as trace
    volume grows.
    """

    def test_compact_many_executions_at_once(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        num_executions = 20
        for i in range(num_executions):
            eid = f"exec-compact-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                num_output_deltas=30,
                num_run_notices=15,
                num_tool_calls=3,
            )
            _seed_thread_and_execution(hub_root, execution_id=eid)
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        summary = compact_completed_execution_history(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=8,
            ),
        )

        assert summary.compacted_executions == num_executions
        assert summary.rows_before > summary.rows_after
        assert summary.rows_after <= num_executions * 8
        assert summary.rows_deleted > 0

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            for i in range(num_executions):
                eid = f"exec-compact-{i:03d}"
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                      FROM orch_event_projections
                     WHERE event_family = 'turn.timeline'
                       AND execution_id = ?
                    """,
                    (eid,),
                ).fetchone()
                hot_count = int(row["cnt"] or 0)
                assert (
                    hot_count <= 9
                ), f"Execution {eid} has {hot_count} hot rows after compaction, expected <= 9"

    def test_compaction_preserves_cold_trace_detail(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        rows = _build_legacy_timeline_rows(
            execution_id="exec-cold-preserve",
            num_output_deltas=40,
            num_run_notices=20,
            num_tool_calls=5,
        )
        _seed_thread_and_execution(hub_root, execution_id="exec-cold-preserve")
        _seed_legacy_timeline_rows(
            hub_root, execution_id="exec-cold-preserve", rows=rows
        )

        backfill_legacy_execution_history(hub_root)

        store = ColdTraceStore(hub_root)
        manifest_before = store.get_manifest("exec-cold-preserve")
        assert manifest_before is not None
        events_before = store.read_events("exec-cold-preserve")
        assert len(events_before) == len(rows)

        compact_completed_execution_history(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=6,
            ),
        )

        manifest_after = store.get_manifest("exec-cold-preserve")
        assert manifest_after is not None
        events_after = store.read_events("exec-cold-preserve")
        assert len(events_after) == len(rows)

    def test_compaction_smoke_completes_quickly(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        num_executions = 10
        for i in range(num_executions):
            eid = f"exec-smoke-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                num_output_deltas=50,
                num_run_notices=30,
                num_tool_calls=5,
            )
            _seed_thread_and_execution(hub_root, execution_id=eid)
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        start = time.monotonic()
        compact_completed_execution_history(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=8,
            ),
        )
        elapsed = time.monotonic() - start

        assert (
            elapsed < 5.0
        ), f"Compaction took {elapsed:.2f}s for {num_executions} executions"

    def test_retention_prune_at_scale(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        recent_started_at = (datetime.now(timezone.utc) - timedelta(hours=12)).replace(
            microsecond=0
        )
        recent_finished_at = recent_started_at + timedelta(minutes=5)
        recent_started_at_iso = recent_started_at.isoformat().replace("+00:00", "Z")
        recent_finished_at_iso = recent_finished_at.isoformat().replace("+00:00", "Z")

        num_old = 15
        num_new = 5
        recent_started_at = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).replace(microsecond=0)
        recent_finished_at = recent_started_at + timedelta(minutes=5)
        for i in range(num_old):
            eid = f"exec-old-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                started_at="2000-01-01T00:00:00Z",
                finished_at="2000-01-01T00:05:00Z",
                num_output_deltas=5,
                num_run_notices=3,
                num_tool_calls=1,
            )
            _seed_thread_and_execution(
                hub_root,
                execution_id=eid,
                started_at="2000-01-01T00:00:00Z",
                finished_at="2000-01-01T00:05:00Z",
            )
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        for i in range(num_new):
            eid = f"exec-new-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                started_at=recent_started_at_iso,
                finished_at=recent_finished_at_iso,
                num_output_deltas=5,
                num_run_notices=3,
                num_tool_calls=1,
            )
            _seed_thread_and_execution(
                hub_root,
                execution_id=eid,
                started_at=recent_started_at_iso,
                finished_at=recent_finished_at_iso,
            )
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        backfill_legacy_execution_history(hub_root)

        store = ColdTraceStore(hub_root)
        old_manifests = [
            store.get_manifest(f"exec-old-{i:03d}") for i in range(num_old)
        ]
        for m in old_manifests:
            assert m is not None

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            with conn:
                for i in range(num_old):
                    eid = f"exec-old-{i:03d}"
                    manifest = store.get_manifest(eid)
                    if manifest:
                        conn.execute(
                            """
                            UPDATE orch_cold_trace_manifests
                               SET started_at = '2000-01-01T00:00:00Z',
                                   finished_at = '2000-01-01T00:05:00Z'
                             WHERE trace_id = ?
                            """,
                            (manifest.trace_id,),
                        )

        summary = prune_execution_history_retention(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=16,
                hot_history_retention_days=1,
                cold_trace_retention_days=1,
            ),
        )

        assert len(summary.pruned_execution_ids) == num_old
        assert summary.hot_rows_deleted > 0

        for i in range(num_new):
            assert store.get_manifest(f"exec-new-{i:03d}") is not None

    def test_vacuum_reclaims_space_after_compaction(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        for i in range(10):
            eid = f"exec-vac-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                num_output_deltas=30,
                num_run_notices=15,
                num_tool_calls=3,
            )
            _seed_thread_and_execution(hub_root, execution_id=eid)
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        compact_completed_execution_history(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=4,
            ),
        )

        summary = vacuum_execution_history(hub_root)
        assert summary.reclaimed_bytes >= 0
        assert summary.size_after <= summary.size_before


class TestStartupRecoveryBoundedBehavior:
    """Startup and recovery must remain bounded as history grows."""

    def test_checkpoint_recovery_does_not_depend_on_hot_rows(
        self, tmp_path: Path
    ) -> None:
        """Recovering execution state from checkpoint must be fast regardless of
        how many hot rows exist for that execution.
        """
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-recover-1")

        events: list[Any] = [
            RunNotice(
                timestamp="2026-04-12T00:00:00Z",
                kind="thinking",
                message="planning",
            ),
            ToolCall(
                timestamp="2026-04-12T00:00:01Z",
                tool_name="shell",
                tool_input={"cmd": "pwd"},
            ),
            ToolResult(
                timestamp="2026-04-12T00:00:02Z",
                tool_name="shell",
                status="completed",
                result={"stdout": "/tmp"},
            ),
            OutputDelta(
                timestamp="2026-04-12T00:00:03Z",
                content="result text",
                delta_type="assistant_message",
            ),
            TokenUsage(
                timestamp="2026-04-12T00:00:04Z",
                usage={"input": 100, "output": 50},
            ),
            Completed(
                timestamp="2026-04-12T00:00:05Z",
                final_message="done",
            ),
        ]
        persist_turn_timeline(
            hub_root,
            execution_id="exec-recover-1",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        store = ColdTraceStore(hub_root)
        checkpoint = store.load_checkpoint("exec-recover-1")
        assert checkpoint is not None
        assert checkpoint.status == "ok"
        assert checkpoint.projection_event_cursor == 6
        assert checkpoint.token_usage == {"input": 100, "output": 50}
        assert len(checkpoint.assistant_text_preview) <= 240
        assert len(checkpoint.terminal_signals) == 1

    def test_recovery_from_checkpoint_is_faster_than_scanning_all_rows(
        self, tmp_path: Path
    ) -> None:
        """Loading a checkpoint should be O(1) relative to hot row count."""
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        num_executions = 50
        for i in range(num_executions):
            eid = f"exec-mass-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                num_output_deltas=20,
                num_run_notices=10,
                num_tool_calls=2,
            )
            _seed_thread_and_execution(hub_root, execution_id=eid)
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        backfill_legacy_execution_history(hub_root)

        target_eid = f"exec-mass-{num_executions - 1:03d}"

        start = time.monotonic()
        store = ColdTraceStore(hub_root)
        checkpoint = store.load_checkpoint(target_eid)
        elapsed = time.monotonic() - start

        assert checkpoint is not None
        assert elapsed < 0.5, f"Checkpoint load took {elapsed:.3f}s"

    def test_diagnostics_scan_time_bounded_at_scale(self, tmp_path: Path) -> None:
        """Full diagnostic scan should not take more than a few seconds even
        with many executions.
        """
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        num_executions = 30
        for i in range(num_executions):
            eid = f"exec-diag-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                num_output_deltas=20,
                num_run_notices=10,
                num_tool_calls=2,
            )
            _seed_thread_and_execution(hub_root, execution_id=eid)
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        backfill_legacy_execution_history(hub_root)

        report = run_execution_history_diagnostics(
            hub_root,
            measure_startup_recovery=True,
        )

        assert report.metrics.total_executions == num_executions
        assert report.metrics.timeline_rows > 0
        assert report.startup_recovery_duration_seconds is not None
        assert (
            report.startup_recovery_duration_seconds < 10.0
        ), f"Diagnostics scan took {report.startup_recovery_duration_seconds:.2f}s"

    def test_metrics_collection_counts_match_actual_rows(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        _seed_thread_and_execution(hub_root, execution_id="exec-metrics-1")

        events: list[Any] = [
            RunNotice(timestamp="2026-04-12T00:00:00Z", kind="thinking", message="m1"),
            OutputDelta(
                timestamp="2026-04-12T00:00:01Z",
                content="text",
                delta_type="assistant_stream",
            ),
            ToolCall(
                timestamp="2026-04-12T00:00:02Z",
                tool_name="shell",
                tool_input={"cmd": "ls"},
            ),
            ToolResult(
                timestamp="2026-04-12T00:00:03Z",
                tool_name="shell",
                status="completed",
                result={"stdout": "file"},
            ),
            TokenUsage(
                timestamp="2026-04-12T00:00:04Z",
                usage={"input": 10, "output": 5},
            ),
            Completed(timestamp="2026-04-12T00:00:05Z", final_message="done"),
        ]
        persist_turn_timeline(
            hub_root,
            execution_id="exec-metrics-1",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        metrics = collect_execution_history_metrics(hub_root)
        assert metrics.total_executions == 1
        assert metrics.terminal_executions == 1
        assert metrics.timeline_rows == 6

        family_counts = metrics.hot_row_count_by_family
        assert family_counts.get("run_notice", 0) >= 1
        assert family_counts.get("output_delta", 0) >= 1
        assert family_counts.get("tool_call", 0) >= 1
        assert family_counts.get("tool_result", 0) >= 1
        assert family_counts.get("token_usage", 0) >= 1
        assert family_counts.get("terminal", 0) >= 1


class TestAmplificationFixture:
    """Scale-oriented fixture reproducing the original amplification pattern.

    The original failure: a long-running execution with hundreds of cumulative
    reasoning updates where each hot row stored the full cumulative text,
    producing quadratic storage growth.

    The fixture creates the pattern with legacy data (as it would have existed
    before the fix) and asserts that compaction and diagnostics correctly
    handle it.
    """

    def test_amplification_pattern_compaction_reduces_to_bounded_hot_rows(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        cumulative = ""
        rows: list[dict[str, Any]] = []
        idx = 1

        rows.append(
            {
                "event_type": "turn_started",
                "timestamp": "2026-04-12T00:00:00Z",
                "status": "recorded",
                "payload": {
                    "event_index": idx,
                    "event_family": "run_notice",
                    "event": {
                        "timestamp": "2026-04-12T00:00:00Z",
                        "kind": "info",
                        "message": "started",
                    },
                },
            }
        )
        idx += 1

        for i in range(200):
            chunk = f"Step {i}: analyzed function foo_{i}(), found issue with variable naming. "
            cumulative += chunk
            rows.append(
                {
                    "event_type": "run_notice",
                    "timestamp": f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    "status": "recorded",
                    "payload": {
                        "event_index": idx,
                        "event_family": "run_notice",
                        "event": {
                            "timestamp": f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                            "kind": "thinking",
                            "message": cumulative,
                        },
                    },
                }
            )
            idx += 1

        for i in range(50):
            rows.append(
                {
                    "event_type": "output_delta",
                    "timestamp": f"2026-04-12T01:{i // 60:02d}:{i % 60:02d}Z",
                    "status": "recorded",
                    "payload": {
                        "event_index": idx,
                        "event_family": "output_delta",
                        "event": {
                            "timestamp": f"2026-04-12T01:{i // 60:02d}:{i % 60:02d}Z",
                            "delta_type": "assistant_stream",
                            "content": f"output text chunk {i} " * 5,
                        },
                    },
                }
            )
            idx += 1

        rows.append(
            {
                "event_type": "token_usage",
                "timestamp": "2026-04-12T02:00:00Z",
                "status": "recorded",
                "payload": {
                    "event_index": idx,
                    "event_family": "token_usage",
                    "event": {
                        "timestamp": "2026-04-12T02:00:00Z",
                        "usage": {"input": 100000, "output": 50000},
                    },
                },
            }
        )
        idx += 1
        rows.append(
            {
                "event_type": "turn_completed",
                "timestamp": "2026-04-12T02:01:00Z",
                "status": "ok",
                "payload": {
                    "event_index": idx,
                    "event_family": "terminal",
                    "event": {
                        "timestamp": "2026-04-12T02:01:00Z",
                        "final_message": "All done.",
                    },
                },
            }
        )

        eid = "exec-amplification"
        _seed_thread_and_execution(
            hub_root,
            execution_id=eid,
            finished_at="2026-04-12T02:01:00Z",
        )
        _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            total_payload_bytes = 0
            for row in conn.execute(
                """
                SELECT payload_json
                  FROM orch_event_projections
                 WHERE event_family = 'turn.timeline'
                   AND execution_id = ?
                """,
                (eid,),
            ).fetchall():
                total_payload_bytes += len(str(row["payload_json"] or ""))

        summary = compact_completed_execution_history(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=8,
            ),
        )

        assert summary.compacted_executions == 1
        assert summary.rows_after <= 9
        assert summary.rows_deleted >= 240

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM orch_event_projections
                 WHERE event_family = 'turn.timeline'
                   AND execution_id = ?
                """,
                (eid,),
            ).fetchone()
            assert int(row["cnt"] or 0) <= 9

        store = ColdTraceStore(hub_root)
        manifest = store.get_manifest(eid)
        assert manifest is not None
        assert manifest.event_count == len(rows)

        cold_events = store.read_events(eid)
        notice_cold = [e for e in cold_events if e["event_type"] == "run_notice"]
        assert len(notice_cold) >= 200

    def test_amplification_diagnostics_detect_oversized_execution(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        rows = _build_legacy_timeline_rows(
            execution_id="exec-oversized",
            num_output_deltas=100,
            num_run_notices=80,
            num_tool_calls=20,
        )
        _seed_thread_and_execution(hub_root, execution_id="exec-oversized")
        _seed_legacy_timeline_rows(hub_root, execution_id="exec-oversized", rows=rows)

        metrics = collect_execution_history_metrics(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=16,
            ),
        )

        assert "exec-oversized" in metrics.oversized_execution_ids
        assert metrics.event_count_by_execution.get("exec-oversized", 0) > 16

    def test_amplification_pattern_live_persist_with_coalescing(
        self, tmp_path: Path
    ) -> None:
        """Live persist_turn_timeline coalesces cumulative reasoning into deltas.

        This is the live-path analog of the legacy fixture above.
        """
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(
            hub_root, execution_id="exec-live-amplification", status="running"
        )

        cumulative = ""
        events: list[Any] = []
        for i in range(150):
            chunk = f"Analyzing module {i}. "
            cumulative += chunk
            events.append(
                RunNotice(
                    timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    kind="thinking",
                    message=cumulative,
                )
            )
        events.append(Completed(timestamp="2026-04-12T03:00:00Z", final_message="done"))

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-live-amplification",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )
        assert count == 151

        timeline = list_turn_timeline(hub_root, execution_id="exec-live-amplification")
        notice_rows = [e for e in timeline if e["event_type"] == "run_notice"]
        assert len(notice_rows) == 100

        total_message_bytes = sum(
            len(e.get("event", {}).get("message", "")) for e in notice_rows
        )
        naive_bytes = sum(len(cumulative) for _ in range(150))
        assert total_message_bytes < naive_bytes, (
            f"Coalesced messages total {total_message_bytes} bytes but "
            f"naive cumulative would be {naive_bytes} bytes"
        )

        checkpoint = ColdTraceStore(hub_root).load_checkpoint("exec-live-amplification")
        assert checkpoint is not None
        assert len(checkpoint.progress_text_preview) <= 240
        assert checkpoint.projection_event_cursor == 151


class TestMigrationBackfillAtScale:
    """Migration and backfill performance smoke tests."""

    def test_backfill_many_executions_smoke(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        num_executions = 15
        for i in range(num_executions):
            eid = f"exec-bf-{i:03d}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                num_output_deltas=25,
                num_run_notices=10,
                num_tool_calls=3,
            )
            _seed_thread_and_execution(hub_root, execution_id=eid)
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        start = time.monotonic()
        summary = backfill_legacy_execution_history(hub_root)
        elapsed = time.monotonic() - start

        assert summary.candidate_executions == num_executions
        assert summary.manifests_created == num_executions
        assert summary.checkpoints_created == num_executions
        assert elapsed < 10.0, f"Backfill took {elapsed:.2f}s"

    def test_backfill_idempotent_on_rerun(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        rows = _build_legacy_timeline_rows(
            execution_id="exec-idem",
            num_output_deltas=10,
            num_run_notices=5,
            num_tool_calls=2,
        )
        _seed_thread_and_execution(hub_root, execution_id="exec-idem")
        _seed_legacy_timeline_rows(hub_root, execution_id="exec-idem", rows=rows)

        first = backfill_legacy_execution_history(hub_root)
        assert first.manifests_created == 1
        assert first.checkpoints_created == 1

        second = backfill_legacy_execution_history(hub_root)
        assert second.manifests_created == 0
        assert second.checkpoints_created == 0
        assert second.candidate_executions == 0

    def test_backfill_creates_valid_checkpoints(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        for status in ("completed", "failed"):
            eid = f"exec-chkpt-{status}"
            rows = _build_legacy_timeline_rows(
                execution_id=eid,
                status=status,
                num_output_deltas=5,
                num_run_notices=3,
                num_tool_calls=1,
            )
            _seed_thread_and_execution(
                hub_root,
                execution_id=eid,
                status=status,
                error_text="test error" if status == "failed" else None,
            )
            _seed_legacy_timeline_rows(hub_root, execution_id=eid, rows=rows)

        backfill_legacy_execution_history(hub_root)

        store = ColdTraceStore(hub_root)
        for status in ("completed", "failed"):
            eid = f"exec-chkpt-{status}"
            checkpoint = store.load_checkpoint(eid)
            assert checkpoint is not None, f"Missing checkpoint for {eid}"
            assert checkpoint.execution_id == eid
            assert checkpoint.status == status
            assert checkpoint.raw_event_count > 0
            assert isinstance(checkpoint.terminal_signals, tuple)

    def test_failed_execution_checkpoint_has_failure_cause(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        events: list[Any] = [
            RunNotice(
                timestamp="2026-04-12T00:00:00Z", kind="progress", message="starting"
            ),
            Failed(
                timestamp="2026-04-12T00:00:01Z", error_message="connection refused"
            ),
        ]
        _seed_thread_and_execution(
            hub_root,
            execution_id="exec-failed-cause",
            status="failed",
            error_text="connection refused",
        )
        persist_turn_timeline(
            hub_root,
            execution_id="exec-failed-cause",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )

        store = ColdTraceStore(hub_root)
        checkpoint = store.load_checkpoint("exec-failed-cause")
        assert checkpoint is not None
        assert checkpoint.status == "error"
        assert checkpoint.failure_cause == "connection refused"


class TestMixedEventScale:
    """Tests with realistic mixes of all event types at scale."""

    def test_persist_mixed_events_respects_all_family_limits(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        _seed_thread_and_execution(hub_root, execution_id="exec-mixed")

        events: list[Any] = []
        for i in range(150):
            events.append(
                RunNotice(
                    timestamp=f"2026-04-12T00:{i // 60:02d}:{i % 60:02d}Z",
                    kind="thinking",
                    message=f"thinking {i}",
                )
            )
        for i in range(250):
            events.append(
                OutputDelta(
                    timestamp=f"2026-04-12T01:{i // 60:02d}:{i % 60:02d}Z",
                    content=f"text {i}",
                    delta_type="assistant_stream",
                )
            )
        for i in range(140):
            events.append(
                ToolCall(
                    timestamp=f"2026-04-12T02:{i // 60:02d}:{i % 60:02d}Z",
                    tool_name=f"tool-{i}",
                    tool_input={"cmd": f"cmd-{i}"},
                )
            )
        for i in range(140):
            events.append(
                ToolResult(
                    timestamp=f"2026-04-12T03:{i // 60:02d}:{i % 60:02d}Z",
                    tool_name=f"tool-{i}",
                    status="completed",
                    result={"out": f"res-{i}"},
                )
            )
        for i in range(80):
            events.append(
                TokenUsage(
                    timestamp=f"2026-04-12T04:{i // 60:02d}:{i % 60:02d}Z",
                    usage={"input": 100 + i, "output": 50 + i},
                )
            )
        events.append(Completed(timestamp="2026-04-12T05:00:00Z", final_message="done"))

        count = persist_turn_timeline(
            hub_root,
            execution_id="exec-mixed",
            target_kind="thread_target",
            target_id="thread-perf-1",
            events=events,
        )
        assert count == 761

        timeline = list_turn_timeline(hub_root, execution_id="exec-mixed")
        by_type: dict[str, int] = {}
        for entry in timeline:
            t = entry["event_type"]
            by_type[t] = by_type.get(t, 0) + 1

        assert by_type.get("run_notice", 0) <= 100
        assert by_type.get("output_delta", 0) <= 200
        assert by_type.get("tool_call", 0) <= 128
        assert by_type.get("tool_result", 0) <= 128
        assert by_type.get("token_usage", 0) <= 64
        assert by_type.get("turn_completed", 0) <= 8

        total_hot = sum(by_type.values())
        assert total_hot <= 628

    def test_mixed_events_after_compaction_stay_bounded(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()

        rows = _build_legacy_timeline_rows(
            execution_id="exec-mixed-compact",
            num_output_deltas=100,
            num_run_notices=50,
            num_tool_calls=10,
        )
        _seed_thread_and_execution(hub_root, execution_id="exec-mixed-compact")
        _seed_legacy_timeline_rows(
            hub_root, execution_id="exec-mixed-compact", rows=rows
        )

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM orch_event_projections
                 WHERE event_family = 'turn.timeline'
                   AND execution_id = ?
                """,
                ("exec-mixed-compact",),
            ).fetchone()
            original_count = int(row["cnt"] or 0)

        compact_completed_execution_history(
            hub_root,
            policy=ExecutionHistoryMaintenancePolicy(
                max_hot_rows_per_completed_execution=8,
            ),
        )

        with open_orchestration_sqlite(hub_root, durable=False) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM orch_event_projections
                 WHERE event_family = 'turn.timeline'
                   AND execution_id = ?
                """,
                ("exec-mixed-compact",),
            ).fetchone()
            compacted_count = int(row["cnt"] or 0)

        assert compacted_count <= 9
        assert compacted_count < original_count

        store = ColdTraceStore(hub_root)
        manifest = store.get_manifest("exec-mixed-compact")
        assert manifest is not None
        assert manifest.event_count == original_count
