"""Ticket-flow CLI read models and human renderers."""

from __future__ import annotations

from typing import Any, Optional

from ....core.flows import FlowStore
from ....core.flows.models import (
    FlowRunRecord,
    flow_run_duration_seconds,
    format_flow_duration,
)
from ....core.flows.ux_helpers import (
    build_flow_status_snapshot,
    format_ticket_flow_app_label,
)
from ....core.runtime import RuntimeContext
from ....core.ticket_flow_operator import PreflightReport


def build_ticket_flow_status_payload(
    engine: RuntimeContext, record: FlowRunRecord, store: Optional[FlowStore]
) -> dict[str, Any]:
    snapshot = build_flow_status_snapshot(engine.repo_root, record, store)
    health = snapshot.get("worker_health")
    effective_ticket = snapshot.get("effective_current_ticket")
    state = record.state if isinstance(record.state, dict) else {}
    raw_ticket_engine = state.get("ticket_engine")
    ticket_engine: dict[str, Any]
    if isinstance(raw_ticket_engine, dict):
        ticket_engine = raw_ticket_engine
    else:
        ticket_engine = {}
    reason_summary = state.get("reason_summary")
    normalized_reason_summary = (
        reason_summary.strip()
        if isinstance(reason_summary, str) and reason_summary.strip()
        else None
    )
    reason = ticket_engine.get("reason")
    normalized_reason = (
        reason.strip() if isinstance(reason, str) and reason.strip() else None
    )
    reason_code = ticket_engine.get("reason_code")
    normalized_reason_code = (
        reason_code.strip()
        if isinstance(reason_code, str) and reason_code.strip()
        else None
    )
    reason_details = ticket_engine.get("reason_details")
    normalized_reason_details = (
        reason_details.strip()
        if isinstance(reason_details, str) and reason_details.strip()
        else None
    )
    error_message = (
        record.error_message.strip()
        if isinstance(record.error_message, str) and record.error_message.strip()
        else None
    )
    run_state = snapshot.get("run_state")
    run_state_payload = run_state if isinstance(run_state, dict) else {}
    return {
        "run_id": record.id,
        "flow_type": record.flow_type,
        "status": record.status.value,
        "current_step": record.current_step,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_seconds": flow_run_duration_seconds(record),
        "last_event_seq": snapshot.get("last_event_seq"),
        "last_event_at": snapshot.get("last_event_at"),
        "effective_last_activity_at": snapshot.get("effective_last_activity_at"),
        "agent": (
            {"status": snapshot.get("agent_status")}
            if snapshot.get("agent_status")
            else None
        ),
        "active_tool": snapshot.get("active_tool"),
        "recovery_state": run_state_payload.get("recovery_state"),
        "worker_status": run_state_payload.get("worker_status"),
        "last_semantic_progress_at": run_state_payload.get("last_semantic_progress_at"),
        "last_tool_activity_at": run_state_payload.get("last_tool_activity_at"),
        "current_phase": run_state_payload.get("current_phase"),
        "stale_reason": run_state_payload.get("stale_reason"),
        "restart_exhausted": run_state_payload.get("restart_exhausted"),
        "freshness": snapshot.get("freshness"),
        "run_state": run_state,
        "current_ticket": effective_ticket,
        "app": snapshot.get("app"),
        "ticket_progress": snapshot.get("ticket_progress"),
        "reason_summary": normalized_reason_summary,
        "reason": normalized_reason,
        "reason_code": normalized_reason_code,
        "reason_details": normalized_reason_details,
        "error_message": error_message,
        "worker": (
            {
                "status": health.status,
                "pid": health.pid,
                "message": health.message,
                "exit_code": getattr(health, "exit_code", None),
                "stderr_tail": getattr(health, "stderr_tail", None),
                "active_tool": snapshot.get("active_tool"),
            }
            if health
            else None
        ),
    }


def render_preflight_report_lines(report: PreflightReport) -> list[str]:
    lines: list[str] = []
    for check in report.checks:
        status = check.status.upper()
        parts = [f"{status}: {check.message}"]
        if check.details:
            parts.append(" ".join(check.details))
        if check.fix:
            parts.append(f"fix: {check.fix}")
        lines.append(" ".join(parts))
    return lines


def render_ticket_flow_status_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    progress = payload.get("ticket_progress") or {}
    tickets = ""
    if isinstance(progress, dict):
        done = progress.get("done")
        total = progress.get("total")
        if isinstance(done, int) and isinstance(total, int):
            tickets = f" tickets={done}/{total}"
    lines.append(
        f"run_id={payload.get('run_id')} status={payload.get('status')}{tickets}"
    )
    step = payload.get("current_step")
    ticket = payload.get("current_ticket") or "n/a"
    lines.append(f"step={step} ticket={ticket}")
    app_label = format_ticket_flow_app_label(payload.get("app"))
    if app_label:
        lines.append(f"app={app_label}")
    lines.append(
        f"created={payload.get('created_at')} started={payload.get('started_at')} "
        f"finished={payload.get('finished_at')}"
    )
    duration_str = format_flow_duration(payload.get("duration_seconds"))
    if duration_str:
        lines.append(f"duration={duration_str}")
    lines.append(
        f"last_event={payload.get('last_event_at')} seq={payload.get('last_event_seq')}"
    )
    effective_last_activity_at = payload.get("effective_last_activity_at")
    if effective_last_activity_at:
        lines.append(f"effective_last_activity={effective_last_activity_at}")
    active_tool = payload.get("active_tool")
    if isinstance(active_tool, dict):
        command = active_tool.get("command")
        if isinstance(command, str) and command.strip():
            detail_parts = []
            elapsed = active_tool.get("elapsed_seconds")
            if isinstance(elapsed, int):
                detail_parts.append(f"running={format_flow_duration(elapsed)}")
            output_updated_at = active_tool.get("output_updated_at")
            if isinstance(output_updated_at, str) and output_updated_at.strip():
                detail_parts.append(f"output_updated={output_updated_at.strip()}")
            suffix = f" {' '.join(detail_parts)}" if detail_parts else ""
            lines.append(f"active_tool: {command.strip()}{suffix}")
    run_state = payload.get("run_state")
    if isinstance(run_state, dict):
        _append_run_state_lines(lines, run_state)
    _append_reason_lines(lines, payload)
    _append_worker_lines(lines, payload)
    return lines


def _append_run_state_lines(lines: list[str], run_state: dict[str, Any]) -> None:
    projection = run_state.get("recovery_projection")
    printed_projection = False
    if isinstance(projection, dict):
        primary = projection.get("primary_state")
        if isinstance(primary, str) and primary.strip():
            lines.append(f"recovery_primary_state: {primary.strip()}")
            printed_projection = True
        facets = projection.get("facets")
        if isinstance(facets, dict):
            for name, raw_facet in facets.items():
                if not isinstance(raw_facet, dict):
                    continue
                status_value = raw_facet.get("status")
                if not isinstance(status_value, str) or status_value == "clear":
                    continue
                reason_value = raw_facet.get("reason")
                reason_suffix = (
                    f" reason={reason_value.strip()}"
                    if isinstance(reason_value, str) and reason_value.strip()
                    else ""
                )
                lines.append(
                    f"recovery_facet: {name} status={status_value}{reason_suffix}"
                )
                printed_projection = True
    if not printed_projection:
        recovery_state = run_state.get("recovery_state")
        if isinstance(recovery_state, str) and recovery_state.strip():
            lines.append(f"recovery_state: {recovery_state.strip()}")
    for key, label in (
        ("worker_status", "worker_status"),
        ("stale_reason", "stale_reason"),
        ("last_semantic_progress_at", "last_semantic_progress_at"),
        ("last_tool_activity_at", "last_tool_activity_at"),
        ("current_phase", "current_phase"),
    ):
        value = run_state.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
    restart_attempts = run_state.get("restart_attempts")
    restart_max = run_state.get("restart_max_attempts")
    if isinstance(restart_attempts, int) and not isinstance(restart_attempts, bool):
        if isinstance(restart_max, int) and not isinstance(restart_max, bool):
            lines.append(f"restart_attempts: {restart_attempts}/{restart_max}")
        else:
            lines.append(f"restart_attempts: {restart_attempts}")
    if run_state.get("commit_barrier_pending"):
        lines.append("commit_barrier: pending preserving completed ticket work")
    last_recovery_action = run_state.get("last_recovery_action")
    if isinstance(last_recovery_action, str) and last_recovery_action.strip():
        lines.append(f"last_recovery_action: {last_recovery_action.strip()}")
    crash_reason = run_state.get("crash_reason") or run_state.get("reap_reason")
    if isinstance(crash_reason, str) and crash_reason.strip():
        lines.append(f"crash_reap_reason: {crash_reason.strip()}")
    recommended = run_state.get("recommended_action")
    if isinstance(recommended, str) and recommended.strip():
        lines.append(f"recommended: {recommended.strip()}")


def _append_reason_lines(lines: list[str], payload: dict[str, Any]) -> None:
    reason_summary = payload.get("reason_summary")
    if isinstance(reason_summary, str) and reason_summary.strip():
        lines.append(f"summary: {reason_summary.strip()}")
    reason_code = payload.get("reason_code")
    if isinstance(reason_code, str) and reason_code.strip():
        lines.append(f"reason_code: {reason_code.strip()}")
    reason = payload.get("reason")
    if (
        isinstance(reason, str)
        and reason.strip()
        and reason.strip() != str(reason_summary or "").strip()
    ):
        lines.append(f"reason: {reason.strip()}")
    reason_details = payload.get("reason_details")
    if isinstance(reason_details, str) and reason_details.strip():
        lines.append(f"reason_details: {reason_details.strip()}")
    error_message = payload.get("error_message")
    if isinstance(error_message, str) and error_message.strip():
        lines.append(f"error: {error_message.strip()}")


def _append_worker_lines(lines: list[str], payload: dict[str, Any]) -> None:
    status = payload.get("status") or ""
    worker = payload.get("worker") or {}
    if not worker:
        return
    if status not in {"completed", "failed", "stopped"}:
        lines.append(f"worker: {worker.get('status')} pid={worker.get('pid')}".rstrip())
        return
    worker_status = worker.get("status") or ""
    worker_msg = worker.get("message") or ""
    if worker_status == "absent" or "missing" in worker_msg.lower():
        lines.append("worker: exited")
    else:
        lines.append(f"worker: {worker.get('status')} pid={worker.get('pid')}".rstrip())
    if status == "failed":
        exit_code = worker.get("exit_code")
        stderr_tail = worker.get("stderr_tail")
        if exit_code is not None:
            lines.append(f"worker_exit={exit_code}")
        if isinstance(stderr_tail, str) and stderr_tail.strip():
            lines.append(f"worker_stderr: {stderr_tail.strip()}")
