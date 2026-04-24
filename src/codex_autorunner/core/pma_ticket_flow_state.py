from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .flows.models import FlowRunRecord, FlowRunStatus
from .flows.store import FlowStore
from .flows.worker_process import check_worker_health
from .ticket_flow_operator import (
    DEFAULT_MAX_TEXT_CHARS,
    TicketFlowRunState,
    TicketFlowWorkerCrash,
)
from .ticket_flow_operator import (
    build_ticket_flow_run_state as _build_ticket_flow_run_state,
)
from .ticket_flow_operator import (
    dispatch_is_actionable as _dispatch_is_actionable_impl,
)
from .ticket_flow_operator import (
    get_latest_ticket_flow_run_state_with_record as _get_latest_run_state_with_record,
)
from .ticket_flow_operator import (
    latest_ticket_flow_dispatch as _latest_ticket_flow_dispatch,
)
from .ticket_flow_operator import (
    latest_ticket_flow_reply_history_seq as _latest_reply_history_seq_impl,
)
from .ticket_flow_operator import (
    resolve_paused_dispatch_state as _resolve_paused_dispatch_state_impl,
)
from .ticket_flow_summary import build_ticket_flow_summary

_logger = logging.getLogger(__name__)

__all__ = [
    "PMA_MAX_TEXT",
    "TicketFlowRunState",
    "TicketFlowWorkerCrash",
    "_dispatch_is_actionable",
    "_get_ticket_flow_summary",
    "_latest_dispatch",
    "_latest_reply_history_seq",
    "_resolve_paused_dispatch_state",
    "_ticket_flow_inbox_item_type_and_next_action",
    "_trim_extra",
    "_truncate",
    "build_ticket_flow_run_state",
    "get_latest_ticket_flow_run_state_with_record",
]

PMA_MAX_TEXT = DEFAULT_MAX_TEXT_CHARS


def _truncate(text: Optional[str], limit: int) -> str:
    raw = text or ""
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."


def _trim_extra(extra: Any, limit: int) -> Any:
    if extra is None:
        return None
    if isinstance(extra, str):
        return _truncate(extra, limit)
    try:
        raw = json.dumps(extra, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(extra)
    if len(raw) <= limit:
        return extra
    return {
        "_omitted": True,
        "note": "extra omitted due to size",
        "preview": _truncate(raw, limit),
    }


def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict[str, Any]]:
    return build_ticket_flow_summary(repo_path, include_failure=False)


def _latest_reply_history_seq(
    repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> int:
    return _latest_reply_history_seq_impl(repo_root, run_id, record_input)


def _dispatch_is_actionable(dispatch_payload: Any) -> bool:
    return _dispatch_is_actionable_impl(dispatch_payload)


def _resolve_paused_dispatch_state(
    *,
    repo_root: Path,
    record_status: FlowRunStatus,
    latest_payload: dict[str, Any],
    latest_reply_seq: int,
) -> tuple[bool, Optional[str]]:
    return _resolve_paused_dispatch_state_impl(
        repo_root=repo_root,
        record_status=record_status,
        latest_payload=latest_payload,
        latest_reply_seq=latest_reply_seq,
    )


def _latest_dispatch(
    repo_root: Path,
    run_id: str,
    input_data: dict[str, Any],
    *,
    max_text_chars: int,
) -> Optional[dict[str, Any]]:
    return _latest_ticket_flow_dispatch(
        repo_root,
        run_id,
        input_data,
        max_text_chars=max_text_chars,
    )


def build_ticket_flow_run_state(
    *,
    repo_root: Path,
    repo_id: str,
    record: FlowRunRecord,
    store: FlowStore,
    has_pending_dispatch: bool,
    dispatch_state_reason: Optional[str] = None,
) -> TicketFlowRunState:
    return _build_ticket_flow_run_state(
        repo_root=repo_root,
        repo_id=repo_id,
        record=record,
        store=store,
        has_pending_dispatch=has_pending_dispatch,
        dispatch_state_reason=dispatch_state_reason,
    )


def get_latest_ticket_flow_run_state_with_record(
    repo_root: Path,
    repo_id: str,
    *,
    store: Optional[FlowStore] = None,
) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
    try:
        return _get_latest_run_state_with_record(
            repo_root,
            repo_id,
            store=store,
            max_text_chars=PMA_MAX_TEXT,
        )
    except Exception as exc:  # intentional: PMA projection must degrade safely
        _logger.warning(
            "Failed to get latest ticket flow run state for repo %s: %s",
            repo_id,
            exc,
        )
        return None, None


def _ticket_flow_inbox_item_type_and_next_action(
    *,
    repo_root: Path,
    record: FlowRunRecord,
) -> tuple[str, str]:
    if record.status == FlowRunStatus.RUNNING:
        health = check_worker_health(repo_root, str(record.id))
        if health.status in {"dead", "invalid", "mismatch"}:
            return "worker_dead", "restart_worker"
    if record.status == FlowRunStatus.FAILED:
        return "run_failed", "diagnose_or_restart"
    if record.status == FlowRunStatus.STOPPED:
        return "run_stopped", "diagnose_or_restart"
    return "run_state_attention", "inspect_and_resume"
