from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from ..flows.ticket_flow.runtime_helpers import ticket_flow_inbox_preflight
from .flows.failure_diagnostics import get_terminal_failure_reason_code
from .flows.models import FlowRunStatus
from .flows.store import FlowStore
from .freshness import resolve_stale_threshold_seconds
from .hub import HubSupervisor
from .hub_inbox_resolution import (
    MESSAGE_PENDING_AUTO_DISMISS_STATE,
    MESSAGE_RESOLVED_STATES,
    clear_message_resolution,
    find_message_resolution,
    find_message_resolution_entry,
    load_hub_inbox_dismissals,
    record_message_pending_auto_dismiss,
    record_message_resolution,
)
from .pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog
from .state_roots import resolve_repo_flows_db_path
from .text_utils import _parse_iso_timestamp
from .ticket_flow_projection import (
    build_canonical_state_v1,
    select_authoritative_run_record,
)

_logger = logging.getLogger(__name__)


def _resolve_inbox_auto_dismiss_grace_seconds(supervisor: HubSupervisor) -> int:
    return max(
        0,
        int(
            getattr(supervisor.hub_config.pma, "inbox_auto_dismiss_grace_seconds", 3600)
        ),
    )


def _record_inbox_auto_dismiss_audit(
    *,
    supervisor: HubSupervisor,
    repo_id: str,
    repo_root: Path,
    run_id: str,
    item_type: str,
    seq: Optional[int],
    reason: str,
    grace_seconds: int,
    reason_code: Optional[str],
    terminal_failure_reason_code: Optional[str],
) -> None:
    try:
        PmaAuditLog(supervisor.hub_config.root).append(
            PmaAuditEntry(
                action_type=PmaActionType.INBOX_AUTO_DISMISSED,
                agent="pma",
                status="ok",
                details={
                    "repo_id": repo_id,
                    "repo_root": str(repo_root),
                    "run_id": run_id,
                    "item_type": item_type,
                    "seq": seq,
                    "grace_seconds": grace_seconds,
                    "reason": reason,
                    "reason_code": reason_code,
                    "terminal_failure_reason_code": terminal_failure_reason_code,
                    "action": "dismiss",
                },
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.warning(
            "Failed to record PMA inbox auto-dismiss audit entry for repo %s run %s: %s",
            repo_id,
            run_id,
            exc,
        )


def _maybe_auto_dismiss_terminal_inbox_item(
    *,
    supervisor: HubSupervisor,
    repo_root: Path,
    repo_id: str,
    dismissals: dict[str, dict[str, Any]],
    record: Any,
    item_type: str,
    seq: Optional[int],
) -> Optional[dict[str, Any]]:
    if item_type not in {"run_failed", "run_stopped"}:
        return None

    run_id = str(record.id)
    terminal_failure_reason = get_terminal_failure_reason_code(record)
    existing_entry = find_message_resolution_entry(
        dismissals,
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        include_unresolved=True,
    )
    preflight = ticket_flow_inbox_preflight(repo_root)
    if preflight.is_recoverable:
        if (
            isinstance(existing_entry, dict)
            and str(existing_entry.get("resolution_state") or "").strip().lower()
            == MESSAGE_PENDING_AUTO_DISMISS_STATE
            and clear_message_resolution(
                repo_root=repo_root,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
            )
        ):
            dismissals.clear()
            dismissals.update(load_hub_inbox_dismissals(repo_root))
        return None

    grace_seconds = _resolve_inbox_auto_dismiss_grace_seconds(supervisor)
    pending = existing_entry
    pending_state = (
        str(pending.get("resolution_state") or "").strip().lower()
        if isinstance(pending, dict)
        else ""
    )
    if pending_state in MESSAGE_RESOLVED_STATES:
        return pending
    pending_reason = (
        str(pending.get("reason") or "").strip() if isinstance(pending, dict) else ""
    )
    expires_at = _parse_iso_timestamp(
        pending.get("expires_at") if isinstance(pending, dict) else None
    )
    if (
        pending_state == MESSAGE_PENDING_AUTO_DISMISS_STATE
        and pending_reason == str(preflight.reason or "").strip()
        and expires_at is not None
    ):
        now = datetime.now(timezone.utc)
        if now >= expires_at:
            reason = (
                f"Auto-dismissed unrecoverable inbox item after {grace_seconds} seconds: "
                f"{preflight.reason}"
            )
            resolved = record_message_resolution(
                repo_root=repo_root,
                repo_id=repo_id,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                action="dismiss",
                reason=reason,
                actor="hub_inbox_auto_dismiss",
            )
            dismissals.clear()
            dismissals.update(load_hub_inbox_dismissals(repo_root))
            _record_inbox_auto_dismiss_audit(
                supervisor=supervisor,
                repo_id=repo_id,
                repo_root=repo_root,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                reason=reason,
                grace_seconds=grace_seconds,
                reason_code=preflight.reason_code,
                terminal_failure_reason_code=(
                    terminal_failure_reason.value
                    if terminal_failure_reason is not None
                    else None
                ),
            )
            _logger.info(
                "Auto-dismissed unrecoverable ticket_flow inbox item for repo %s run %s (%s)",
                repo_id,
                run_id,
                preflight.reason,
            )
            return resolved
        return None

    if grace_seconds == 0:
        reason = (
            f"Auto-dismissed unrecoverable inbox item immediately: {preflight.reason}"
        )
        resolved = record_message_resolution(
            repo_root=repo_root,
            repo_id=repo_id,
            run_id=run_id,
            item_type=item_type,
            seq=seq,
            action="dismiss",
            reason=reason,
            actor="hub_inbox_auto_dismiss",
        )
        dismissals.clear()
        dismissals.update(load_hub_inbox_dismissals(repo_root))
        _record_inbox_auto_dismiss_audit(
            supervisor=supervisor,
            repo_id=repo_id,
            repo_root=repo_root,
            run_id=run_id,
            item_type=item_type,
            seq=seq,
            reason=reason,
            grace_seconds=grace_seconds,
            reason_code=preflight.reason_code,
            terminal_failure_reason_code=(
                terminal_failure_reason.value
                if terminal_failure_reason is not None
                else None
            ),
        )
        _logger.info(
            "Auto-dismissed unrecoverable ticket_flow inbox item for repo %s run %s (%s)",
            repo_id,
            run_id,
            preflight.reason,
        )
        return resolved

    record_message_pending_auto_dismiss(
        repo_root=repo_root,
        repo_id=repo_id,
        run_id=run_id,
        item_type=item_type,
        seq=seq,
        reason=preflight.reason,
        grace_seconds=grace_seconds,
        actor="hub_inbox_auto_dismiss",
    )
    dismissals.clear()
    dismissals.update(load_hub_inbox_dismissals(repo_root))
    _logger.info(
        "Scheduled auto-dismiss for unrecoverable ticket_flow inbox item in repo %s run %s after %s seconds (%s)",
        repo_id,
        run_id,
        grace_seconds,
        preflight.reason,
    )
    return None


def _inbox_resolve_repos(
    supervisor: HubSupervisor,
) -> list[Any]:
    try:
        snapshots = supervisor.list_repos()
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.warning("Could not list repos for inbox: %s", exc)
        return []
    return [snap for snap in snapshots if snap.initialized and snap.exists_on_disk]


def _inbox_load_repo_flow_context(
    snap: Any,
    store: Any,
    preferred_run_id: str,
) -> dict[str, Any]:
    dismissals = load_hub_inbox_dismissals(snap.path)
    active_statuses = [
        FlowRunStatus.PAUSED,
        FlowRunStatus.RUNNING,
        FlowRunStatus.FAILED,
        FlowRunStatus.STOPPED,
    ]
    all_runs = store.list_flow_runs(flow_type="ticket_flow")
    newest_record = select_authoritative_run_record(
        all_runs, preferred_run_id=preferred_run_id
    )
    newest_run_id = str(newest_record.id) if newest_record else None
    active_run_id: Optional[str] = None
    if newest_record and newest_record.status in (
        FlowRunStatus.RUNNING,
        FlowRunStatus.PAUSED,
    ):
        active_run_id = str(newest_record.id)
    return {
        "dismissals": dismissals,
        "active_statuses": active_statuses,
        "all_runs": all_runs,
        "newest_run_id": newest_run_id,
        "active_run_id": active_run_id,
    }


def _inbox_resolve_run_dispatch(
    repo_root: Path,
    record_id: str,
    record_input: dict[str, Any],
    record_status: Any,
    max_text_chars: int,
) -> dict[str, Any]:
    from .pma_context import (
        _latest_dispatch,
        _latest_reply_history_seq,
        _resolve_paused_dispatch_state,
    )

    latest = _latest_dispatch(
        repo_root, record_id, record_input, max_text_chars=max_text_chars
    )
    latest_payload = latest if isinstance(latest, dict) else {}
    latest_reply_seq = _latest_reply_history_seq(repo_root, record_id, record_input)
    seq = int(latest_payload.get("seq") or 0)
    dispatch_payload = latest_payload.get("dispatch")
    has_dispatch, dispatch_state_reason = _resolve_paused_dispatch_state(
        repo_root=repo_root,
        record_status=record_status,
        latest_payload=latest_payload,
        latest_reply_seq=latest_reply_seq,
    )
    return {
        "latest_payload": latest_payload,
        "seq": seq,
        "dispatch_payload": dispatch_payload,
        "has_dispatch": has_dispatch,
        "dispatch_state_reason": dispatch_state_reason,
    }


def _inbox_build_base_item(
    snap: Any,
    record: Any,
    run_state: Any,
    active_run_id: Optional[str],
    repo_root: Path,
    repo_id: str,
    newest_run_id: Optional[str],
    store: Any,
    stale_threshold_seconds: int,
) -> dict[str, Any]:
    record_id = str(record.id)
    return {
        "repo_id": repo_id,
        "repo_display_name": snap.display_name,
        "run_id": record.id,
        "run_created_at": record.created_at,
        "status": record.status.value,
        "open_url": f"/repos/{repo_id}/?tab=inbox&run_id={record_id}",
        "run_state": run_state,
        "canonical_state_v1": build_canonical_state_v1(
            repo_root=repo_root,
            repo_id=repo_id,
            run_state=cast(dict[str, Any], run_state),
            record=record,
            store=store,
            preferred_run_id=newest_run_id,
            stale_threshold_seconds=stale_threshold_seconds,
        ),
        "active_run_id": active_run_id,
    }


def _inbox_build_dispatch_item(
    base_item: dict[str, Any],
    *,
    seq: int,
    dispatch_payload: Any,
    latest_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        **base_item,
        "item_type": "run_dispatch",
        "next_action": "reply_and_resume",
        "seq": seq,
        "dispatch": dispatch_payload,
        "dispatch_actionable": True,
        "files": latest_payload.get("files") or [],
    }


def _inbox_build_non_dispatch_item(
    base_item: dict[str, Any],
    *,
    supervisor: HubSupervisor,
    snap: Any,
    repo_root: Path,
    record: Any,
    dismissals: dict[str, dict[str, Any]],
    seq: int,
    latest_payload: dict[str, Any],
    dispatch_payload: Any,
    dispatch_state_reason: Optional[str],
    run_state: Any,
) -> Optional[dict[str, Any]]:
    from .pma_context import _ticket_flow_inbox_item_type_and_next_action

    record_id = str(record.id)
    item_type, next_action = _ticket_flow_inbox_item_type_and_next_action(
        repo_root=repo_root, record=record
    )
    _maybe_auto_dismiss_terminal_inbox_item(
        supervisor=supervisor,
        repo_root=repo_root,
        repo_id=snap.id,
        dismissals=dismissals,
        record=record,
        item_type=item_type,
        seq=seq if seq > 0 else None,
    )
    if find_message_resolution(
        dismissals,
        run_id=record_id,
        item_type=item_type,
        seq=seq if seq > 0 else None,
    ):
        return None
    item_resolution = find_message_resolution_entry(
        dismissals,
        run_id=record_id,
        item_type=item_type,
        seq=seq if seq > 0 else None,
        include_unresolved=True,
    )
    item_reason = dispatch_state_reason
    if (
        isinstance(item_resolution, dict)
        and str(item_resolution.get("resolution_state") or "").strip().lower()
        == MESSAGE_PENDING_AUTO_DISMISS_STATE
    ):
        pending_reason = str(item_resolution.get("reason") or "").strip()
        if pending_reason:
            item_reason = pending_reason
    return {
        **base_item,
        "item_type": item_type,
        "next_action": next_action,
        "seq": seq if seq > 0 else None,
        "dispatch": dispatch_payload,
        "dispatch_actionable": False,
        "files": latest_payload.get("files") or [],
        "reason": item_reason,
        "available_actions": run_state.get("recommended_actions", []),
    }


def _inbox_process_run(
    supervisor: HubSupervisor,
    snap: Any,
    repo_root: Path,
    record: Any,
    store: Any,
    dismissals: dict[str, dict[str, Any]],
    active_run_id: Optional[str],
    newest_run_id: Optional[str],
    max_text_chars: int,
    stale_threshold_seconds: int,
) -> Optional[dict[str, Any]]:
    from .pma_context import build_ticket_flow_run_state

    record_id = str(record.id)
    record_input = dict(record.input_data or {})

    dispatch = _inbox_resolve_run_dispatch(
        repo_root, record_id, record_input, record.status, max_text_chars
    )

    is_terminal_failed = record.status in (
        FlowRunStatus.FAILED,
        FlowRunStatus.STOPPED,
    )
    if is_terminal_failed and active_run_id and active_run_id != record_id:
        return None

    dispatch_state_reason = dispatch["dispatch_state_reason"]
    if record.status == FlowRunStatus.FAILED:
        dispatch_state_reason = record.error_message or "Run failed"
    elif record.status == FlowRunStatus.STOPPED:
        dispatch_state_reason = "Run was stopped"

    run_state = build_ticket_flow_run_state(
        repo_root=repo_root,
        repo_id=snap.id,
        record=record,
        store=store,
        has_pending_dispatch=dispatch["has_dispatch"],
        dispatch_state_reason=dispatch_state_reason,
    )
    run_state["active_run_id"] = active_run_id

    if not run_state.get("attention_required") and not is_terminal_failed:
        if dispatch["has_dispatch"]:
            pass
        else:
            return None

    base_item = _inbox_build_base_item(
        snap,
        record,
        run_state,
        active_run_id,
        repo_root,
        snap.id,
        newest_run_id,
        store,
        stale_threshold_seconds,
    )

    seq = dispatch["seq"]
    if dispatch["has_dispatch"]:
        if find_message_resolution(
            dismissals,
            run_id=record_id,
            item_type="run_dispatch",
            seq=seq if seq > 0 else None,
        ):
            return None
        return _inbox_build_dispatch_item(
            base_item,
            seq=seq,
            dispatch_payload=dispatch["dispatch_payload"],
            latest_payload=dispatch["latest_payload"],
        )

    return _inbox_build_non_dispatch_item(
        base_item,
        supervisor=supervisor,
        snap=snap,
        repo_root=repo_root,
        record=record,
        dismissals=dismissals,
        seq=seq,
        latest_payload=dispatch["latest_payload"],
        dispatch_payload=dispatch["dispatch_payload"],
        dispatch_state_reason=dispatch_state_reason,
        run_state=run_state,
    )


def _gather_inbox(
    supervisor: HubSupervisor,
    *,
    max_text_chars: int,
    stale_threshold_seconds: Optional[int] = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    stale_threshold_seconds = resolve_stale_threshold_seconds(stale_threshold_seconds)
    repos = _inbox_resolve_repos(supervisor)
    for snap in repos:
        repo_root = snap.path
        db_path = resolve_repo_flows_db_path(repo_root)
        if not db_path.exists():
            continue
        try:
            with FlowStore.connect_readonly(db_path) as store:
                ctx = _inbox_load_repo_flow_context(
                    snap, store, str(snap.last_run_id or "")
                )
                for record in ctx["all_runs"]:
                    if record.status not in ctx["active_statuses"]:
                        continue
                    record_id = str(record.id)
                    if ctx["newest_run_id"] and record_id != ctx["newest_run_id"]:
                        continue
                    item = _inbox_process_run(
                        supervisor,
                        snap,
                        repo_root,
                        record,
                        store,
                        ctx["dismissals"],
                        ctx["active_run_id"],
                        ctx["newest_run_id"],
                        max_text_chars,
                        stale_threshold_seconds,
                    )
                    if item is not None:
                        messages.append(item)
        except (OSError, RuntimeError, ValueError) as exc:
            _logger.warning("Failed to gather inbox for repo %s: %s", snap.id, exc)
            continue
    messages.sort(key=lambda m: m.get("run_created_at") or "", reverse=True)
    return messages
