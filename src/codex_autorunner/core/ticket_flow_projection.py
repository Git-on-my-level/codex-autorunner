from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..tickets.files import list_ticket_paths
from ..tickets.frontmatter import parse_markdown_frontmatter
from ..tickets.ingest_state import read_ingest_receipt
from .config import load_repo_config
from .flows.models import FlowRunRecord
from .flows.store import FlowStore

_START_NEW_FLOW_TOKEN = " flow ticket_flow start "
_COMPLETED_FLOW_STATUSES = {"completed", "done"}
_ATTENTION_STATES = {"blocked", "dead", "paused"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_optional_int(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _select_newest_run(
    records: list[FlowRunRecord],
    *,
    preferred_run_id: Optional[str] = None,
    represented_run_id: Optional[str] = None,
) -> Optional[FlowRunRecord]:
    if not records:
        return None
    if represented_run_id:
        represented = next(
            (r for r in records if str(r.id) == represented_run_id), None
        )
        if represented is not None:
            return represented
    if preferred_run_id:
        preferred = next((r for r in records if str(r.id) == preferred_run_id), None)
        if preferred is not None:
            return preferred
    return max(
        records,
        key=lambda r: (
            str(r.created_at or ""),
            str(r.started_at or ""),
            str(r.finished_at or ""),
            str(r.id),
        ),
    )


def _is_start_new_flow_action(action: str) -> bool:
    normalized = f" {action.strip().lower()} "
    return _START_NEW_FLOW_TOKEN in normalized and " --run-id " not in normalized


def _collect_ticket_frontmatter_state(repo_root: Path) -> dict[str, Any]:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    try:
        ticket_paths = list_ticket_paths(ticket_dir)
    except Exception:
        ticket_paths = []

    total_count = len(ticket_paths)
    done_count = 0
    effective_next_ticket: Optional[str] = None

    for path in ticket_paths:
        done_flag = False
        try:
            raw = path.read_text(encoding="utf-8")
            frontmatter, _ = parse_markdown_frontmatter(raw)
            if isinstance(frontmatter, dict) and isinstance(
                frontmatter.get("done"), bool
            ):
                done_flag = frontmatter["done"]
        except Exception:
            done_flag = False

        if done_flag:
            done_count += 1
        elif effective_next_ticket is None:
            effective_next_ticket = path.name

    return {
        "frontmatter_total_count": total_count,
        "frontmatter_done_count": done_count,
        "effective_next_ticket": effective_next_ticket,
    }


def _resolve_ingest_state(
    repo_root: Path,
    *,
    frontmatter_total_count: int,
) -> tuple[bool, Optional[str], str]:
    receipt = read_ingest_receipt(repo_root)
    if receipt is not None:
        source = _normalize_optional_str(receipt.get("source")) or "ticket_files"
        return True, _normalize_optional_str(receipt.get("ingested_at")), source
    return frontmatter_total_count > 0, None, "ticket_files"


def _resolve_last_event_meta(
    *,
    repo_root: Path,
    record: Optional[FlowRunRecord],
    store: Optional[FlowStore],
    preferred_run_id: Optional[str],
    represented_run_id: Optional[str],
) -> tuple[Optional[FlowRunRecord], Optional[int], Optional[str]]:
    if record is not None:
        if store is None:
            return record, None, None
        seq, event_at = store.get_last_event_meta(str(record.id))
        return (
            record,
            _normalize_optional_int(seq),
            _normalize_optional_str(event_at),
        )

    if store is not None:
        records = store.list_flow_runs(flow_type="ticket_flow")
        latest = _select_newest_run(
            records,
            preferred_run_id=preferred_run_id,
            represented_run_id=represented_run_id,
        )
        if latest is None:
            return None, None, None
        seq, event_at = store.get_last_event_meta(str(latest.id))
        return (
            latest,
            _normalize_optional_int(seq),
            _normalize_optional_str(event_at),
        )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None, None, None

    try:
        config = load_repo_config(repo_root)
        with FlowStore(db_path, durable=config.durable_writes) as local_store:
            records = local_store.list_flow_runs(flow_type="ticket_flow")
            latest = _select_newest_run(
                records,
                preferred_run_id=preferred_run_id,
                represented_run_id=represented_run_id,
            )
            if latest is None:
                return None, None, None
            seq, event_at = local_store.get_last_event_meta(str(latest.id))
            return (
                latest,
                _normalize_optional_int(seq),
                _normalize_optional_str(event_at),
            )
    except Exception:
        return None, None, None


def build_canonical_state_v1(
    *,
    repo_root: Path,
    repo_id: str,
    run_state: Optional[dict[str, Any]],
    record: Optional[FlowRunRecord] = None,
    store: Optional[FlowStore] = None,
    preferred_run_id: Optional[str] = None,
) -> dict[str, Any]:
    observed_at = _iso_now()
    ticket_state = _collect_ticket_frontmatter_state(repo_root)
    frontmatter_total_count = int(ticket_state["frontmatter_total_count"])
    frontmatter_done_count = int(ticket_state["frontmatter_done_count"])
    effective_next_ticket = _normalize_optional_str(
        ticket_state["effective_next_ticket"]
    )

    run_state_payload = run_state if isinstance(run_state, dict) else {}
    run_state_run_id = _normalize_optional_str(run_state_payload.get("run_id"))
    record_run_id = (
        _normalize_optional_str(getattr(record, "id", None))
        if record is not None
        else None
    )
    represented_run_id = record_run_id or run_state_run_id

    latest_record, last_event_seq, last_event_at = _resolve_last_event_meta(
        repo_root=repo_root,
        record=record,
        store=store,
        preferred_run_id=preferred_run_id,
        represented_run_id=represented_run_id,
    )

    latest_run_id = (
        _normalize_optional_str(getattr(latest_record, "id", None))
        if latest_record is not None
        else represented_run_id
    )
    latest_status_raw = (
        getattr(getattr(latest_record, "status", None), "value", None)
        if latest_record is not None
        else run_state_payload.get("flow_status")
    )
    latest_run_status = _normalize_optional_str(latest_status_raw)

    state = _normalize_optional_str(run_state_payload.get("state"))
    if state is None:
        state = latest_run_status

    blocking_reason = _normalize_optional_str(run_state_payload.get("blocking_reason"))
    flow_current_ticket = _normalize_optional_str(
        run_state_payload.get("current_ticket")
    )

    attention_required_raw = run_state_payload.get("attention_required")
    if isinstance(attention_required_raw, bool):
        attention_required = attention_required_raw
    else:
        attention_required = bool(state in _ATTENTION_STATES)

    recommended_actions: list[str] = []
    raw_actions = run_state_payload.get("recommended_actions")
    if isinstance(raw_actions, list):
        for candidate in raw_actions:
            action = _normalize_optional_str(candidate)
            if action:
                recommended_actions.append(action)

    recommended_action = _normalize_optional_str(
        run_state_payload.get("recommended_action")
    )
    if recommended_action and recommended_action not in recommended_actions:
        recommended_actions.insert(0, recommended_action)
    elif not recommended_action and recommended_actions:
        recommended_action = recommended_actions[0]

    ingested, ingested_at, ingest_source = _resolve_ingest_state(
        repo_root,
        frontmatter_total_count=frontmatter_total_count,
    )
    completed_by_flow = bool(
        state == "completed"
        or (
            latest_run_status is not None
            and latest_run_status in _COMPLETED_FLOW_STATUSES
        )
    )

    recommendation_stale_reason = None
    if (
        recommended_action
        and _is_start_new_flow_action(recommended_action)
        and effective_next_ticket
    ):
        recommendation_stale_reason = (
            "recommended_action_stale:start_new_flow_while_next_ticket_exists"
        )

    contradictions: list[str] = []
    if run_state_run_id and record_run_id and run_state_run_id != record_run_id:
        contradictions.append("run_state_run_id_mismatch_record")
    if represented_run_id and latest_run_id and represented_run_id != latest_run_id:
        contradictions.append("represented_run_mismatch_latest")
    if latest_run_id and not ingested:
        contradictions.append("run_exists_without_ticket_ingest")
    if completed_by_flow and effective_next_ticket:
        contradictions.append("completed_flow_with_remaining_tickets")
    if attention_required and not recommended_actions:
        contradictions.append("attention_required_without_recommendation")

    recommendation_confidence = "high"
    if recommendation_stale_reason:
        recommendation_confidence = "low"
    elif contradictions:
        recommendation_confidence = "medium"

    return {
        "schema_version": 1,
        "observed_at": observed_at,
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "ingested": ingested,
        "ingested_at": ingested_at,
        "ingest_source": ingest_source,
        "represented_run_id": represented_run_id,
        "frontmatter_total_count": frontmatter_total_count,
        "frontmatter_done_count": frontmatter_done_count,
        "effective_next_ticket": effective_next_ticket,
        "latest_run_id": latest_run_id,
        "latest_run_status": latest_run_status,
        "completed_by_flow": completed_by_flow,
        "flow_current_ticket": flow_current_ticket,
        "last_event_seq": last_event_seq,
        "last_event_at": last_event_at,
        "state": state,
        "blocking_reason": blocking_reason,
        "attention_required": attention_required,
        "recommended_action": recommended_action,
        "recommended_actions": recommended_actions,
        "recommendation_generated_at": observed_at,
        "recommendation_confidence": recommendation_confidence,
        "recommendation_stale_reason": recommendation_stale_reason,
        "contradictions": contradictions,
    }
