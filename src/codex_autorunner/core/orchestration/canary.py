from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, cast

from fastapi.testclient import TestClient

from ...bootstrap import seed_hub_files
from ..pma_thread_store import PmaThreadStore
from .bindings import OrchestrationBindingStore
from .cold_trace_store import ColdTraceStore
from .execution_history import ExecutionCheckpoint
from .execution_history_diagnostics import (
    log_quarantine,
    log_startup_recovery,
    run_execution_history_diagnostics,
)
from .execution_history_maintenance import (
    audit_execution_history,
    backfill_legacy_execution_history,
    compact_completed_execution_history,
    resolve_execution_history_maintenance_policy,
    vacuum_execution_history,
)
from .sqlite import open_orchestration_sqlite, resolve_orchestration_sqlite_path

_CREATE_HUB_APP: Callable[[Path], Any]


def _round_duration(value: float) -> float:
    return round(max(float(value), 0.0), 6)


def _seed_execution(
    hub_root: Path,
    *,
    execution_id: str,
    thread_target_id: str,
    repo_id: str,
    status: str,
    started_at: str,
    finished_at: str,
    output_chunks: int,
    notice_count: int,
    tool_call_count: int,
) -> None:
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
                    backend_thread_id = excluded.backend_thread_id,
                    updated_at = excluded.updated_at
                """,
                (
                    thread_target_id,
                    "codex",
                    f"backend-{thread_target_id}",
                    repo_id,
                    "repo",
                    repo_id,
                    str(hub_root / "workspace"),
                    f"Canary {execution_id}",
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
                    thread_target_id,
                    f"client-{execution_id}",
                    "message",
                    "Canary execution prompt",
                    status,
                    f"backend-turn-{execution_id}",
                    "",
                    "simulated failure" if status == "failed" else None,
                    "gpt-canary",
                    "high",
                    None,
                    started_at,
                    finished_at,
                    started_at,
                ),
            )

            event_index = 1

            def _insert_row(
                *,
                event_type: str,
                timestamp: str,
                event_status: str,
                payload: dict[str, Any],
            ) -> None:
                nonlocal event_index
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
                        f"turn-timeline:{execution_id}:{event_index:04d}",
                        "turn.timeline",
                        event_type,
                        "thread_target",
                        thread_target_id,
                        execution_id,
                        repo_id,
                        "repo",
                        repo_id,
                        None,
                        timestamp,
                        event_status,
                        json.dumps(payload),
                        1,
                    ),
                )
                event_index += 1

            _insert_row(
                event_type="turn_started",
                timestamp=started_at,
                event_status="recorded",
                payload={
                    "event_index": event_index,
                    "event_family": "run_notice",
                    "event": {
                        "timestamp": started_at,
                        "kind": "info",
                        "message": "started",
                    },
                },
            )
            for notice_index in range(notice_count):
                timestamp = f"2026-04-12T00:00:{notice_index % 60:02d}Z"
                _insert_row(
                    event_type="run_notice",
                    timestamp=timestamp,
                    event_status="recorded",
                    payload={
                        "event_index": event_index,
                        "event_family": "run_notice",
                        "event": {
                            "timestamp": timestamp,
                            "kind": "thinking" if notice_index % 2 == 0 else "progress",
                            "message": f"notice {notice_index}: " + ("n" * 120),
                        },
                    },
                )
            for tool_index in range(tool_call_count):
                call_timestamp = f"2026-04-12T00:01:{(tool_index * 2) % 60:02d}Z"
                result_timestamp = f"2026-04-12T00:01:{(tool_index * 2 + 1) % 60:02d}Z"
                _insert_row(
                    event_type="tool_call",
                    timestamp=call_timestamp,
                    event_status="recorded",
                    payload={
                        "event_index": event_index,
                        "event_family": "tool_call",
                        "event": {
                            "timestamp": call_timestamp,
                            "tool_name": f"tool-{tool_index}",
                            "tool_input": {"cmd": "echo canary", "payload": "x" * 256},
                        },
                    },
                )
                _insert_row(
                    event_type="tool_result",
                    timestamp=result_timestamp,
                    event_status="completed",
                    payload={
                        "event_index": event_index,
                        "event_family": "tool_result",
                        "event": {
                            "timestamp": result_timestamp,
                            "tool_name": f"tool-{tool_index}",
                            "status": "completed",
                            "result": {"stdout": "ok", "payload": "y" * 256},
                        },
                    },
                )
            for chunk_index in range(output_chunks):
                timestamp = f"2026-04-12T00:02:{chunk_index % 60:02d}Z"
                _insert_row(
                    event_type="output_delta",
                    timestamp=timestamp,
                    event_status="recorded",
                    payload={
                        "event_index": event_index,
                        "event_family": "output_delta",
                        "event": {
                            "timestamp": timestamp,
                            "delta_type": "assistant_message",
                            "content": f"chunk-{chunk_index} " + ("d" * 96),
                        },
                    },
                )
            _insert_row(
                event_type="token_usage",
                timestamp="2026-04-12T00:04:00Z",
                event_status="recorded",
                payload={
                    "event_index": event_index,
                    "event_family": "token_usage",
                    "event": {
                        "timestamp": "2026-04-12T00:04:00Z",
                        "usage": {"input": 2000, "output": 1200},
                    },
                },
            )
            _insert_row(
                event_type="turn_failed" if status == "failed" else "turn_completed",
                timestamp=finished_at,
                event_status="error" if status == "failed" else "ok",
                payload={
                    "event_index": event_index,
                    "event_family": "terminal",
                    "event": {
                        "timestamp": finished_at,
                        "final_message": "done" if status != "failed" else "",
                        "error_message": (
                            "simulated failure" if status == "failed" else ""
                        ),
                    },
                },
            )


def _seed_canary_history(
    hub_root: Path,
    *,
    execution_count: int,
    output_chunks: int,
    notice_count: int,
    tool_call_count: int,
) -> dict[str, Any]:
    repo_root = hub_root / "workspace"
    repo_root.mkdir(parents=True, exist_ok=True)
    heavy_execution_id = "exec-canary-heavy"
    failed_execution_id = "exec-canary-failed"
    execution_ids: list[str] = []
    for index in range(execution_count):
        execution_id = heavy_execution_id if index == 0 else f"exec-canary-{index:03d}"
        execution_ids.append(execution_id)
        _seed_execution(
            hub_root,
            execution_id=execution_id,
            thread_target_id=f"thread-canary-{index:03d}",
            repo_id="repo-canary",
            status="completed",
            started_at="2026-04-12T00:00:00Z",
            finished_at="2026-04-12T00:05:00Z",
            output_chunks=output_chunks,
            notice_count=notice_count,
            tool_call_count=tool_call_count,
        )
    _seed_execution(
        hub_root,
        execution_id=failed_execution_id,
        thread_target_id="thread-canary-failed",
        repo_id="repo-canary",
        status="failed",
        started_at="2026-04-12T01:00:00Z",
        finished_at="2026-04-12T01:05:00Z",
        output_chunks=max(output_chunks // 2, 12),
        notice_count=max(notice_count // 2, 8),
        tool_call_count=max(tool_call_count // 2, 2),
    )
    execution_ids.append(failed_execution_id)
    return {
        "execution_ids": execution_ids,
        "heavy_execution_id": heavy_execution_id,
        "failed_execution_id": failed_execution_id,
        "repo_root": str(repo_root),
    }


def _measure_hub_startup(hub_root: Path) -> dict[str, Any]:
    start = time.monotonic()
    with TestClient(_CREATE_HUB_APP(hub_root)) as client:
        response = client.get("/health")
        body = response.json()
    return {
        "duration_seconds": _round_duration(time.monotonic() - start),
        "status_code": response.status_code,
        "health_status": body.get("status"),
    }


def _run_trace_validation(hub_root: Path, *, execution_id: str) -> dict[str, Any]:
    with TestClient(_CREATE_HUB_APP(hub_root)) as client:
        page = client.get(f"/hub/pma/traces/events/{execution_id}?limit=5")
        page_body = page.json()
        trace = client.get(f"/hub/pma/traces/events/{execution_id}?limit=200")
        trace_body = trace.json()
        checkpoint = client.get(f"/hub/pma/traces/checkpoint/{execution_id}")
        checkpoint_body = checkpoint.json()
    families = sorted(
        {
            str(event.get("event_family") or "")
            for event in trace_body.get("events", [])
            if isinstance(event, dict)
        }
    )
    trace_manifest = checkpoint_body.get("trace_manifest") or {}
    return {
        "page_status_code": page.status_code,
        "page_limit": page_body.get("limit"),
        "page_count": len(page_body.get("events", [])),
        "total_count": page_body.get("total_count"),
        "has_more": page_body.get("has_more"),
        "families": families,
        "checkpoint_status_code": checkpoint.status_code,
        "checkpoint_status": checkpoint_body.get("checkpoint", {}).get("status"),
        "checkpoint_trace_manifest_id": checkpoint_body.get("checkpoint", {}).get(
            "trace_manifest_id"
        ),
        "trace_manifest_id": trace_manifest.get("trace_id"),
        "trace_manifest_event_count": trace_manifest.get("event_count"),
    }


async def _run_recovery_validation(
    hub_root: Path,
    *,
    workspace_root: Path,
    recover_orphaned_executions: Callable[[Any], Awaitable[None]],
) -> dict[str, Any]:
    with TestClient(_CREATE_HUB_APP(hub_root)) as client:
        app = cast(Any, client.app)
        store = PmaThreadStore(hub_root)
        binding_store = OrchestrationBindingStore(hub_root)
        cold_store = ColdTraceStore(hub_root)

        pma_thread = store.create_thread(
            "codex", workspace_root.resolve(), repo_id="repo-canary"
        )
        pma_thread_id = str(pma_thread["managed_thread_id"])
        pma_turn = store.create_turn(pma_thread_id, prompt="recover pma turn")
        pma_turn_id = str(pma_turn["managed_turn_id"])
        store.set_thread_backend_id(pma_thread_id, None)
        store.set_turn_backend_turn_id(pma_turn_id, None)
        cold_store.save_checkpoint(
            ExecutionCheckpoint(
                execution_id=pma_turn_id,
                thread_target_id=pma_thread_id,
                status="running",
                backend_thread_id="checkpoint-thread-pma",
                backend_turn_id="checkpoint-turn-pma",
                last_progress_at="2026-04-12T02:00:00Z",
            )
        )

        discord_thread = store.create_thread(
            "codex",
            workspace_root.resolve(),
            repo_id="repo-canary",
        )
        discord_thread_id = str(discord_thread["managed_thread_id"])
        discord_turn = store.create_turn(
            discord_thread_id, prompt="recover discord turn"
        )
        discord_turn_id = str(discord_turn["managed_turn_id"])
        store.set_thread_backend_id(discord_thread_id, None)
        store.set_turn_backend_turn_id(discord_turn_id, None)
        binding_store.upsert_binding(
            surface_kind="discord",
            surface_key="channel-canary",
            thread_target_id=discord_thread_id,
            agent_id="codex",
            repo_id="repo-canary",
        )
        cold_store.save_checkpoint(
            ExecutionCheckpoint(
                execution_id=discord_turn_id,
                thread_target_id=discord_thread_id,
                status="running",
                backend_thread_id="checkpoint-thread-discord",
                backend_turn_id="checkpoint-turn-discord",
                last_progress_at="2026-04-12T02:01:00Z",
            )
        )

        async def _has_turn(thread_id: str, turn_id: str) -> bool:
            return (thread_id, turn_id) in {
                ("checkpoint-thread-pma", "checkpoint-turn-pma"),
                ("checkpoint-thread-discord", "checkpoint-turn-discord"),
            }

        app.state.app_server_events = SimpleNamespace(has_turn=_has_turn)
        start = time.monotonic()
        await recover_orphaned_executions(app)
        duration = time.monotonic() - start

        pma_updated_turn = store.get_turn(pma_thread_id, pma_turn_id) or {}
        pma_binding = store.get_thread_runtime_binding(pma_thread_id)
        discord_updated_turn = store.get_turn(discord_thread_id, discord_turn_id) or {}
        discord_binding = store.get_thread_runtime_binding(discord_thread_id)

    return {
        "duration_seconds": _round_duration(duration),
        "pma_checkpoint_restore": {
            "thread_id": pma_thread_id,
            "turn_id": pma_turn_id,
            "status": pma_updated_turn.get("status"),
            "backend_turn_id": pma_updated_turn.get("backend_turn_id"),
            "backend_thread_id": (
                getattr(pma_binding, "backend_thread_id", None) if pma_binding else None
            ),
        },
        "discord_bound_checkpoint_restore": {
            "thread_id": discord_thread_id,
            "turn_id": discord_turn_id,
            "status": discord_updated_turn.get("status"),
            "backend_turn_id": discord_updated_turn.get("backend_turn_id"),
            "backend_thread_id": (
                getattr(discord_binding, "backend_thread_id", None)
                if discord_binding
                else None
            ),
        },
    }


def run_execution_history_canary(
    hub_root: Path,
    *,
    create_hub_app: Callable[[Path], Any],
    recover_orphaned_executions: Callable[[Any], Awaitable[None]],
    execution_count: int = 12,
    output_chunks: int = 60,
    notice_count: int = 24,
    tool_call_count: int = 4,
) -> dict[str, Any]:
    seed_hub_files(hub_root, force=True)
    seed = _seed_canary_history(
        hub_root,
        execution_count=execution_count,
        output_chunks=output_chunks,
        notice_count=notice_count,
        tool_call_count=tool_call_count,
    )
    sqlite_path = resolve_orchestration_sqlite_path(hub_root)
    sqlite_bytes_before = sqlite_path.stat().st_size if sqlite_path.exists() else 0
    global _CREATE_HUB_APP
    _CREATE_HUB_APP = create_hub_app

    app = create_hub_app(hub_root)
    policy = resolve_execution_history_maintenance_policy(app.state.config.pma)
    before_audit = audit_execution_history(hub_root, policy=policy).to_dict()
    migration = backfill_legacy_execution_history(hub_root).to_dict()
    compaction = compact_completed_execution_history(
        hub_root,
        policy=policy,
    ).to_dict()
    after_audit = audit_execution_history(hub_root, policy=policy).to_dict()
    vacuum = vacuum_execution_history(hub_root).to_dict()
    sqlite_bytes_after = sqlite_path.stat().st_size if sqlite_path.exists() else 0
    diagnostics_logger = logging.getLogger(
        "codex_autorunner.execution_history_diagnostics"
    )
    diagnostics_logger_disabled = diagnostics_logger.disabled
    diagnostics_logger.disabled = True
    try:
        diagnostics = run_execution_history_diagnostics(
            hub_root,
            policy=policy,
            measure_startup_recovery=True,
        ).to_dict()
    finally:
        diagnostics_logger.disabled = diagnostics_logger_disabled
    startup = _measure_hub_startup(hub_root)
    recovery = asyncio.run(
        _run_recovery_validation(
            hub_root,
            workspace_root=Path(str(seed["repo_root"])),
            recover_orphaned_executions=recover_orphaned_executions,
        )
    )
    recovered_sections = (
        recovery.get("pma_checkpoint_restore") or {},
        recovery.get("discord_bound_checkpoint_restore") or {},
    )
    recovered_count = sum(
        1 for section in recovered_sections if section.get("backend_turn_id")
    )
    log_startup_recovery(
        duration_seconds=float(recovery.get("duration_seconds") or 0.0),
        executions_recovered=recovered_count,
        checkpoints_loaded=recovered_count,
    )
    threshold_breaches = diagnostics.get("threshold_breaches") or []
    if threshold_breaches:
        log_quarantine(
            execution_id=str(seed["heavy_execution_id"]),
            reason="execution_history_threshold_breaches",
            context={"threshold_breaches": threshold_breaches},
        )
    trace_validation = _run_trace_validation(
        hub_root,
        execution_id=str(seed["heavy_execution_id"]),
    )
    return {
        "hub_root": str(hub_root),
        "policy": policy.to_dict(),
        "seed": seed,
        "before": {
            "sqlite_bytes": sqlite_bytes_before,
            "audit": before_audit,
        },
        "migration": migration,
        "compaction": compaction,
        "after": {
            "sqlite_bytes": sqlite_bytes_after,
            "audit": after_audit,
            "vacuum": vacuum,
            "diagnostics": diagnostics,
        },
        "startup": startup,
        "recovery": recovery,
        "trace_validation": trace_validation,
    }


__all__ = ["run_execution_history_canary"]
