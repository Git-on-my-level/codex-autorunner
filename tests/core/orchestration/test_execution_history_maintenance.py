from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import (
    execution_history_maintenance as maintenance_module,
)
from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceStore
from codex_autorunner.core.orchestration.execution_history import ExecutionCheckpoint
from codex_autorunner.core.orchestration.execution_history_maintenance import (
    ExecutionHistoryMaintenancePolicy,
    audit_execution_history,
    backfill_legacy_execution_history,
    compact_completed_execution_history,
    export_execution_history_bundle,
    prune_execution_history_retention,
)
from codex_autorunner.core.orchestration.sqlite import (
    initialize_orchestration_sqlite,
    open_orchestration_sqlite,
)
from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
from codex_autorunner.core.ports.run_event import RunNotice


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
                    thread_target_id,
                    agent_id,
                    backend_thread_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    workspace_root,
                    display_name,
                    lifecycle_status,
                    runtime_status,
                    created_at,
                    updated_at
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
                    execution_id,
                    thread_target_id,
                    client_request_id,
                    request_kind,
                    prompt_text,
                    status,
                    backend_turn_id,
                    assistant_text,
                    error_text,
                    model_id,
                    reasoning_level,
                    transcript_mirror_id,
                    started_at,
                    finished_at,
                    created_at
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
                    "boom" if status == "failed" else None,
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
                                "error_message": "boom" if status == "failed" else "",
                            },
                        },
                    ),
                ]
            )
            for event_id, event_type, timestamp, event_status, payload in rows:
                conn.execute(
                    """
                    INSERT INTO orch_event_projections (
                        event_id,
                        event_family,
                        event_type,
                        target_kind,
                        target_id,
                        execution_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        run_id,
                        timestamp,
                        status,
                        payload_json,
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


def test_backfill_legacy_execution_history_creates_manifest_and_checkpoint(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-legacy", output_chunks=4)

    summary = backfill_legacy_execution_history(hub_root)

    assert summary.candidate_executions == 1
    assert summary.manifests_created == 1
    assert summary.checkpoints_created == 1

    store = ColdTraceStore(hub_root)
    manifest = store.get_manifest("exec-legacy")
    checkpoint = store.load_checkpoint("exec-legacy")
    events = store.read_events("exec-legacy")

    assert manifest is not None
    assert manifest.event_count == 10
    assert checkpoint is not None
    assert checkpoint.trace_manifest_id == manifest.trace_id
    assert checkpoint.assistant_text_preview.startswith("chunk-0")
    assert checkpoint.token_usage == {"input": 12, "output": 7}
    assert checkpoint.raw_event_count == 10
    assert len(checkpoint.terminal_signals) == 1
    assert len(events) == 10
    assert events[2]["event_family"] == "tool_call"


def test_compact_completed_execution_history_reduces_hot_rows_and_keeps_cold_trace(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-compact", output_chunks=20)

    summary = compact_completed_execution_history(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=8,
            hot_history_retention_days=30,
            cold_trace_retention_days=90,
        ),
    )

    assert summary.compacted_executions == 1
    assert summary.rows_before > summary.rows_after
    assert summary.rows_after <= 8
    assert summary.rows_deleted > 0

    store = ColdTraceStore(hub_root)
    manifest = store.get_manifest("exec-compact")
    assert manifest is not None
    assert manifest.event_count == 26

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        count_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = 'turn.timeline'
               AND execution_id = 'exec-compact'
            """
        ).fetchone()
        summary_row = conn.execute(
            """
            SELECT payload_json
              FROM orch_event_projections
             WHERE execution_id = 'exec-compact'
               AND event_id LIKE '%:compaction-summary'
            """
        ).fetchone()

    assert int(count_row["cnt"] or 0) <= 8
    assert summary_row is not None
    payload = json.loads(str(summary_row["payload_json"]))
    assert payload["event"]["kind"] == "compaction_summary"
    assert payload["event"]["data"]["removed_hot_rows"] > 0


def test_compaction_skips_small_executions_without_loading_timeline_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-small", output_chunks=1)

    def fail_load(*_args, **_kwargs):
        raise AssertionError("small executions should be skipped by count precheck")

    monkeypatch.setattr(maintenance_module, "_load_timeline_rows", fail_load)

    summary = compact_completed_execution_history(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=16
        ),
    )

    assert summary.compacted_executions == 0
    assert summary.rows_deleted == 0


def test_compaction_precheck_ignores_summary_rows_for_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    execution_id = "exec-summary-threshold"
    policy = ExecutionHistoryMaintenancePolicy(max_hot_rows_per_completed_execution=8)
    _seed_execution(hub_root, execution_id=execution_id, output_chunks=20)

    first_summary = compact_completed_execution_history(hub_root, policy=policy)
    assert first_summary.compacted_executions == 1

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id,
                    event_family,
                    event_type,
                    target_kind,
                    target_id,
                    execution_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    run_id,
                    timestamp,
                    status,
                    payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"turn-timeline:{execution_id}:9999",
                    "turn.timeline",
                    "run_notice",
                    "thread_target",
                    "thread-1",
                    execution_id,
                    "repo-1",
                    "repo",
                    "repo-1",
                    None,
                    "2026-04-12T00:05:30Z",
                    "recorded",
                    json.dumps(
                        {
                            "event_index": 9999,
                            "event_family": "run_notice",
                            "event": {
                                "timestamp": "2026-04-12T00:05:30Z",
                                "kind": "progress",
                                "message": "post-compaction follow-up",
                            },
                        }
                    ),
                    1,
                ),
            )

        total_rows = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = 'turn.timeline'
               AND execution_id = ?
            """,
            (execution_id,),
        ).fetchone()
        baseline_rows = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = 'turn.timeline'
               AND execution_id = ?
               AND event_id NOT LIKE '%:compaction-summary'
            """,
            (execution_id,),
        ).fetchone()

    assert int(total_rows["cnt"] or 0) == 9
    assert int(baseline_rows["cnt"] or 0) == 8

    def fail_load(*_args, **_kwargs):
        raise AssertionError("summary rows should not force a second compaction pass")

    monkeypatch.setattr(maintenance_module, "_load_timeline_rows", fail_load)

    second_summary = compact_completed_execution_history(hub_root, policy=policy)

    assert second_summary.compacted_executions == 0
    assert second_summary.rows_deleted == 0


def test_prune_execution_history_retention_removes_old_hot_rows_and_traces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(
        hub_root,
        execution_id="exec-old",
        started_at="2000-01-01T00:00:00Z",
        finished_at="2000-01-01T00:05:00Z",
        output_chunks=2,
    )
    _seed_execution(hub_root, execution_id="exec-new", output_chunks=2)
    backfill_legacy_execution_history(hub_root)

    store = ColdTraceStore(hub_root)
    old_manifest = store.get_manifest("exec-old")
    new_manifest = store.get_manifest("exec-new")
    assert old_manifest is not None
    assert new_manifest is not None

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_cold_trace_manifests
                   SET started_at = '2000-01-01T00:00:00Z',
                       finished_at = '2000-01-01T00:05:00Z'
                 WHERE execution_id = 'exec-old'
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO orch_execution_checkpoints (
                    execution_id,
                    thread_target_id,
                    status,
                    checkpoint_json,
                    trace_manifest_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "exec-old",
                    "thread-1",
                    "completed",
                    json.dumps(
                        ExecutionCheckpoint(
                            status="completed",
                            execution_id="exec-old",
                            thread_target_id="thread-1",
                            trace_manifest_id=old_manifest.trace_id,
                        ).to_dict()
                    ),
                    old_manifest.trace_id,
                    "2000-01-01T00:05:00Z",
                    "2000-01-01T00:05:00Z",
                ),
            )

    old_artifact = (
        hub_root / ".codex-autorunner" / "traces" / old_manifest.artifact_relpath
    )
    assert old_artifact.exists()

    original_iter_manifests = ColdTraceStore.iter_manifests
    original_iter_checkpoints = ColdTraceStore.iter_checkpoints

    def _iter_manifests_paged(self, **kwargs):
        kwargs.setdefault("page_size", 1)
        yield from original_iter_manifests(self, **kwargs)

    def _iter_checkpoints_paged(self, **kwargs):
        kwargs.setdefault("page_size", 1)
        yield from original_iter_checkpoints(self, **kwargs)

    monkeypatch.setattr(ColdTraceStore, "iter_manifests", _iter_manifests_paged)
    monkeypatch.setattr(ColdTraceStore, "iter_checkpoints", _iter_checkpoints_paged)

    summary = prune_execution_history_retention(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=16,
            hot_history_retention_days=1,
            cold_trace_retention_days=1,
        ),
    )

    assert "exec-old" in summary.pruned_execution_ids
    assert old_manifest.trace_id in summary.pruned_trace_ids
    assert summary.hot_rows_deleted > 0
    assert summary.checkpoints_deleted >= 1
    assert summary.manifests_deleted >= 1
    assert summary.trace_files_deleted >= 1

    assert store.get_manifest("exec-old") is None
    assert store.load_checkpoint("exec-old") is None
    assert not old_artifact.exists()
    assert store.get_manifest("exec-new") is not None


def test_prune_execution_history_retention_clears_checkpoint_trace_manifest_json(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(
        hub_root,
        execution_id="exec-old-json",
        started_at="2000-01-01T00:00:00Z",
        finished_at="2000-01-01T00:05:00Z",
        output_chunks=2,
    )
    backfill_legacy_execution_history(hub_root)

    store = ColdTraceStore(hub_root)
    manifest = store.get_manifest("exec-old-json")
    assert manifest is not None

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_cold_trace_manifests
                   SET started_at = '2000-01-01T00:00:00Z',
                       finished_at = '2000-01-01T00:05:00Z'
                 WHERE execution_id = 'exec-old-json'
                """
            )

    prune_execution_history_retention(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=16,
            hot_history_retention_days=10_000,
            cold_trace_retention_days=1,
        ),
    )

    checkpoint = store.load_checkpoint("exec-old-json")
    assert checkpoint is not None
    assert checkpoint.trace_manifest_id is None


def test_compaction_never_keeps_more_than_policy_limit(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-tight-limit", output_chunks=20)

    summary = compact_completed_execution_history(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=4,
            hot_history_retention_days=30,
            cold_trace_retention_days=90,
        ),
    )

    assert summary.compacted_executions == 1
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = 'turn.timeline'
               AND execution_id = 'exec-tight-limit'
            """
        ).fetchone()
    assert int(row["cnt"] or 0) <= 4


def test_compaction_refreshes_checkpoint_hot_state_after_reducing_hot_rows(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-refresh-state", output_chunks=20)

    compact_completed_execution_history(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=8,
            hot_history_retention_days=30,
            cold_trace_retention_days=90,
        ),
    )

    checkpoint = ColdTraceStore(hub_root).load_checkpoint("exec-refresh-state")
    assert checkpoint is not None
    assert checkpoint.hot_projection_state is not None
    assert sum(checkpoint.hot_projection_state["family_hot_rows"].values()) <= 8

    persist_turn_timeline(
        hub_root,
        execution_id="exec-refresh-state",
        target_kind="thread_target",
        target_id="thread-1",
        events=[
            RunNotice(
                timestamp="2026-04-12T00:06:00Z",
                kind="progress",
                message="follow-up after compaction",
            )
        ],
        start_index=999,
    )

    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM orch_event_projections
             WHERE event_family = 'turn.timeline'
               AND execution_id = 'exec-refresh-state'
            """
        ).fetchone()
    assert int(row["cnt"] or 0) <= 9


def test_audit_flags_recent_terminal_execution_without_manifest_even_with_zero_hot_rows(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    initialize_orchestration_sqlite(hub_root, durable=False)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO orch_thread_targets (
                    thread_target_id,
                    agent_id,
                    backend_thread_id,
                    repo_id,
                    resource_kind,
                    resource_id,
                    workspace_root,
                    display_name,
                    lifecycle_status,
                    runtime_status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "thread-zero-hot",
                    "codex",
                    "backend-thread-zero-hot",
                    "repo-1",
                    "repo",
                    "repo-1",
                    str(hub_root / "workspace"),
                    "Zero hot",
                    "active",
                    "completed",
                    "2026-04-12T00:00:00Z",
                    "2026-04-12T00:05:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO orch_thread_executions (
                    execution_id,
                    thread_target_id,
                    client_request_id,
                    request_kind,
                    prompt_text,
                    status,
                    backend_turn_id,
                    assistant_text,
                    error_text,
                    model_id,
                    reasoning_level,
                    transcript_mirror_id,
                    started_at,
                    finished_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "exec-zero-hot",
                    "thread-zero-hot",
                    "client-zero-hot",
                    "message",
                    "Summarize",
                    "completed",
                    "backend-turn-zero-hot",
                    "done",
                    None,
                    "gpt-test",
                    "high",
                    None,
                    "2026-04-12T00:00:00Z",
                    "2026-04-12T00:05:00Z",
                    "2026-04-12T00:00:00Z",
                ),
            )

    summary = audit_execution_history(
        hub_root,
        policy=ExecutionHistoryMaintenancePolicy(
            max_hot_rows_per_completed_execution=16,
            hot_history_retention_days=30,
            cold_trace_retention_days=90,
        ),
    )

    assert "exec-zero-hot" in summary.missing_manifest_execution_ids


def test_export_execution_history_bundle_copies_traces_and_metadata(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _seed_execution(hub_root, execution_id="exec-export", output_chunks=2)
    backfill_legacy_execution_history(hub_root)

    export_root = tmp_path / "export"
    summary = export_execution_history_bundle(hub_root, export_root=export_root)

    assert summary.execution_count == 1
    assert summary.manifest_count == 1
    assert summary.checkpoint_count == 1
    assert summary.trace_file_count == 1

    manifests = json.loads((export_root / "manifests.json").read_text(encoding="utf-8"))
    checkpoints = json.loads(
        (export_root / "checkpoints.json").read_text(encoding="utf-8")
    )
    assert manifests[0]["execution_id"] == "exec-export"
    assert checkpoints[0]["execution_id"] == "exec-export"
    exported_trace = export_root / "traces" / manifests[0]["artifact_relpath"]
    assert exported_trace.exists()
