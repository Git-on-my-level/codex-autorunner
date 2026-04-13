from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .config import load_repo_config
from .flows import FlowStore
from .flows.failure_diagnostics import format_failure_summary, get_failure_payload
from .flows.models import FlowRunRecord
from .ticket_flow_projection import (
    TicketFlowCensus,
    collect_ticket_flow_census,
    select_authoritative_run_record,
)

_FLOW_STATUS_ICONS = {
    "running": "🟢",
    "pending": "🟡",
    "stopping": "🟡",
    "paused": "🔴",
    "completed": "🔵",
    "done": "🔵",
    "failed": "⚫",
    "stopped": "⚫",
    "superseded": "⚫",
    "idle": "⚪",
}
_ACTIVE_FLOW_STATUSES = {"running", "pending", "paused", "stopping"}


def get_latest_ticket_flow_run(store: FlowStore) -> Optional[FlowRunRecord]:
    records = store.list_flow_runs(flow_type="ticket_flow")
    return select_authoritative_run_record(records)


def _load_latest_ticket_flow_run(repo_path: Path) -> Optional[FlowRunRecord]:
    db_path = repo_path / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    config = load_repo_config(repo_path)
    with FlowStore(db_path, durable=config.durable_writes) as store:
        return get_latest_ticket_flow_run(store)


def _load_latest_ticket_flow_run_from_store(
    store: FlowStore,
) -> Optional[FlowRunRecord]:
    return get_latest_ticket_flow_run(store)


def build_ticket_flow_display(
    *,
    status: Optional[str],
    done_count: int,
    total_count: int,
    run_id: Optional[str],
) -> dict[str, Any]:
    done = max(int(done_count or 0), 0)
    total = max(int(total_count or 0), 0)
    normalized = str(status or "").strip().lower()

    if normalized:
        effective_status = normalized
        status_label = normalized
    else:
        completed_without_run = total > 0 and done >= total
        effective_status = "done" if completed_without_run else "idle"
        status_label = "Done" if completed_without_run else "Idle"

    return {
        "status": effective_status,
        "status_label": status_label,
        "status_icon": _FLOW_STATUS_ICONS.get(effective_status, "⚪"),
        "is_active": effective_status in _ACTIVE_FLOW_STATUSES,
        "done_count": done,
        "total_count": total,
        "run_id": run_id,
    }


def format_ticket_flow_summary_lines(summary: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []

    status_label = summary.get("status_label")
    if isinstance(status_label, str) and status_label.strip():
        lines.append(f"Status: {status_label.strip()}")

    done_count = summary.get("done_count")
    total_count = summary.get("total_count")
    if (
        isinstance(done_count, int)
        and isinstance(total_count, int)
        and total_count >= 0
    ):
        lines.append(f"Tickets: {done_count}/{total_count}")

    current_step = summary.get("current_step")
    if isinstance(current_step, int):
        lines.append(f"Step: {current_step}")
    elif isinstance(current_step, str) and current_step.strip():
        lines.append(f"Step: {current_step.strip()}")

    run_id = summary.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        lines.append(f"Run: {run_id.strip()}")

    return lines


def build_ticket_flow_summary(
    repo_path: Path,
    *,
    include_failure: bool,
    store: Optional[FlowStore] = None,
    census: Optional[TicketFlowCensus] = None,
) -> Optional[dict[str, Any]]:
    if census is None:
        census = collect_ticket_flow_census(repo_path)
    if census.total_count == 0:
        return None

    total_count = census.total_count
    done_count = census.done_count
    pr_url = census.open_pr_url
    final_review_status = census.final_review_status

    try:
        latest = (
            _load_latest_ticket_flow_run_from_store(store)
            if store is not None
            else _load_latest_ticket_flow_run(repo_path)
        )
    except (
        Exception
    ):  # intentional: summary degrades gracefully on any data-access failure
        return None

    display = build_ticket_flow_display(
        status=latest.status.value if latest else None,
        done_count=done_count,
        total_count=total_count,
        run_id=latest.id if latest else None,
    )

    state = latest.state if latest and isinstance(latest.state, dict) else {}
    engine = state.get("ticket_engine") if isinstance(state, dict) else {}
    engine = engine if isinstance(engine, dict) else {}
    current_step = engine.get("total_turns")

    summary: dict[str, Any] = {
        "status": display["status"],
        "status_label": display["status_label"],
        "status_icon": display["status_icon"],
        "run_id": display["run_id"],
        "done_count": display["done_count"],
        "total_count": display["total_count"],
        "current_step": current_step,
        "pr_url": pr_url,
        "pr_opened": bool(pr_url),
        "final_review_status": final_review_status,
    }
    if include_failure:
        failure_payload = get_failure_payload(latest) if latest else None
        summary["failure"] = failure_payload
        summary["failure_summary"] = (
            format_failure_summary(failure_payload) if failure_payload else None
        )
    return summary
