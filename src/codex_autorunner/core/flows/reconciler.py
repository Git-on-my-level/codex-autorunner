from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..locks import FileLockBusy, file_lock
from .failure_diagnostics import ensure_failure_payload
from .models import FlowEventType, FlowRunRecord, FlowRunStatus
from .store import UNSET, FlowStore
from .transition import resolve_flow_transition
from .worker_process import FlowWorkerHealth, check_worker_health, clear_worker_metadata

_logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (
    FlowRunStatus.RUNNING,
    FlowRunStatus.STOPPING,
    FlowRunStatus.PAUSED,
)


@dataclass
class FlowReconcileSummary:
    checked: int = 0
    active: int = 0
    updated: int = 0
    locked: int = 0
    errors: int = 0


@dataclass
class FlowReconcileResult:
    records: list[FlowRunRecord]
    summary: FlowReconcileSummary


def _reconcile_lock_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".codex-autorunner" / "flows" / run_id / "reconcile.lock"


def _ensure_worker_not_stale(health: FlowWorkerHealth) -> None:
    if health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata(health.artifact_path.parent)
        except Exception:
            _logger.debug("Failed to clear worker metadata: %s", health.artifact_path)


def reconcile_flow_run(
    repo_root: Path,
    record: FlowRunRecord,
    store: FlowStore,
    *,
    logger: Optional[logging.Logger] = None,
) -> tuple[FlowRunRecord, bool, bool]:
    if record.status not in _ACTIVE_STATUSES:
        return record, False, False

    lock_path = _reconcile_lock_path(repo_root, record.id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(lock_path, blocking=False):
            health = check_worker_health(repo_root, record.id)
            decision = resolve_flow_transition(record, health)

            if (
                decision.status == record.status
                and decision.finished_at == record.finished_at
                and decision.state == (record.state or {})
                and decision.error_message == record.error_message
            ):
                return record, False, False

            (logger or _logger).info(
                "Reconciling flow %s: %s -> %s (%s)",
                record.id,
                record.status.value,
                decision.status.value,
                decision.note or "reconcile",
            )

            state = decision.state
            if decision.status == FlowRunStatus.FAILED:
                state = ensure_failure_payload(
                    state,
                    record=record,
                    step_id=record.current_step,
                    error_message=decision.error_message,
                    store=store,
                    note=decision.note,
                    failed_at=decision.finished_at,
                )
            updated = store.update_flow_run_status(
                run_id=record.id,
                status=decision.status,
                state=state,
                finished_at=decision.finished_at if decision.finished_at else UNSET,
                error_message=decision.error_message,
            )

            if decision.status == FlowRunStatus.FAILED and decision.error_message:
                try:
                    store.create_event(
                        event_id=str(uuid.uuid4()),
                        run_id=record.id,
                        event_type=FlowEventType.FLOW_FAILED,
                        data={
                            "error": decision.error_message,
                            "reason": decision.note or "reconcile",
                        },
                    )
                except Exception as exc:
                    (logger or _logger).warning(
                        "Failed to emit flow_failed event for %s: %s", record.id, exc
                    )

            _ensure_worker_not_stale(health)
            return (updated or record), bool(updated), False
    except FileLockBusy:
        return record, False, True
    except Exception as exc:
        (logger or _logger).warning("Failed to reconcile flow %s: %s", record.id, exc)
        return record, False, False


def reconcile_flow_runs(
    repo_root: Path,
    *,
    flow_type: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> FlowReconcileResult:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return FlowReconcileResult(records=[], summary=FlowReconcileSummary())
    from ..config import load_repo_config

    config = load_repo_config(repo_root)
    store = FlowStore(db_path, durable=config.durable_writes)
    summary = FlowReconcileSummary()
    records: list[FlowRunRecord] = []
    try:
        store.initialize()
        for record in store.list_flow_runs(flow_type=flow_type):
            if record.status in _ACTIVE_STATUSES:
                summary.active += 1
                summary.checked += 1
                record, updated, locked = reconcile_flow_run(
                    repo_root, record, store, logger=logger
                )
                if updated:
                    summary.updated += 1
                if locked:
                    summary.locked += 1
            records.append(record)
    except Exception as exc:
        summary.errors += 1
        (logger or _logger).warning("Flow reconcile run failed: %s", exc)
    finally:
        try:
            store.close()
        except Exception:
            pass
    return FlowReconcileResult(records=records, summary=summary)
