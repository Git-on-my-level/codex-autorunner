from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..tickets.files import list_ticket_paths
from ..tickets.frontmatter import parse_markdown_frontmatter
from ..tickets.ingest_state import read_ingest_receipt
from .config import load_repo_config
from .flows.models import FlowRunRecord, FlowRunStatus
from .flows.store import FlowStore
from .freshness import build_freshness_payload
from .text_utils import _iso_now

_START_NEW_FLOW_TOKEN = " flow ticket_flow start "
_COMPLETED_FLOW_STATUSES = {"completed", "done"}
_ATTENTION_STATES = {"blocked", "dead", "paused"}
_PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)


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


@dataclass(frozen=True)
class TicketFlowCensus:
    total_count: int
    done_count: int
    effective_next_ticket: Optional[str]
    open_pr_url: Optional[str]
    final_review_status: Optional[str]


@dataclass(frozen=True)
class AuthoritativeRunFacts:
    record: Optional[FlowRunRecord]
    last_event_seq: Optional[int]
    last_event_at: Optional[str]


def select_authoritative_run_record(
    records: list[FlowRunRecord],
    *,
    preferred_run_id: Optional[str] = None,
    represented_run_id: Optional[str] = None,
) -> Optional[FlowRunRecord]:
    if not records:
        return None
    ordered_records = list(records)
    if represented_run_id:
        represented = next(
            (r for r in ordered_records if str(r.id) == represented_run_id), None
        )
        if represented is not None:
            return represented
    candidates = [
        record
        for record in ordered_records
        if record.status != FlowRunStatus.SUPERSEDED
    ] or ordered_records
    latest = candidates[0]
    if preferred_run_id:
        preferred = next((r for r in candidates if str(r.id) == preferred_run_id), None)
        if preferred is not None and preferred.id == latest.id:
            return preferred
        if (
            preferred is not None
            and preferred.status
            in {
                FlowRunStatus.RUNNING,
                FlowRunStatus.STOPPING,
            }
            and latest.status
            in {
                FlowRunStatus.PAUSED,
                FlowRunStatus.FAILED,
                FlowRunStatus.STOPPED,
            }
        ):
            return preferred
    return latest


def _is_start_new_flow_action(action: str) -> bool:
    normalized = f" {action.strip().lower()} "
    return _START_NEW_FLOW_TOKEN in normalized and " --run-id " not in normalized


def _extract_pr_url(data: dict[str, Any], body: Optional[str]) -> Optional[str]:
    frontmatter_pr = data.get("pr_url")
    if isinstance(frontmatter_pr, str) and frontmatter_pr.strip():
        return frontmatter_pr.strip()
    if body:
        match = _PR_URL_RE.search(body)
        if match:
            return match.group(0)
    return None


def collect_ticket_flow_census(repo_root: Path) -> TicketFlowCensus:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    try:
        ticket_paths = list_ticket_paths(ticket_dir)
    except (OSError, ValueError):
        ticket_paths = []

    total_count = len(ticket_paths)
    done_count = 0
    effective_next_ticket: Optional[str] = None
    open_pr_url: Optional[str] = None
    final_review_status: Optional[str] = None

    for path in ticket_paths:
        done_flag = False
        frontmatter: Any = None
        body: Optional[str] = None
        try:
            raw = path.read_text(encoding="utf-8")
            frontmatter, body = parse_markdown_frontmatter(raw)
        except (OSError, ValueError):
            done_flag = False

        if isinstance(frontmatter, dict) and isinstance(frontmatter.get("done"), bool):
            done_flag = frontmatter["done"]

        if done_flag:
            done_count += 1
        elif effective_next_ticket is None:
            effective_next_ticket = path.name

        if not isinstance(frontmatter, dict):
            continue

        title = str(frontmatter.get("title") or "").strip().lower()
        ticket_kind = str(frontmatter.get("ticket_kind") or "").strip().lower()

        is_final_review = ticket_kind == "final_review" or "final review" in title
        if is_final_review:
            final_review_status = "done" if done_flag else "pending"

        is_open_pr = (
            ticket_kind == "open_pr" or "open pr" in title or "pull request" in title
        )
        if is_open_pr and open_pr_url is None:
            pr = _extract_pr_url(frontmatter, body)
            if pr is not None:
                open_pr_url = pr

    return TicketFlowCensus(
        total_count=total_count,
        done_count=done_count,
        effective_next_ticket=effective_next_ticket,
        open_pr_url=open_pr_url,
        final_review_status=final_review_status,
    )


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


def resolve_authoritative_ticket_flow_run(
    repo_root: Path,
    *,
    store: Optional[FlowStore] = None,
    preferred_run_id: Optional[str] = None,
    represented_run_id: Optional[str] = None,
) -> Optional[FlowRunRecord]:
    if store is not None:
        records = store.list_flow_runs(flow_type="ticket_flow")
        return select_authoritative_run_record(
            records,
            preferred_run_id=preferred_run_id,
            represented_run_id=represented_run_id,
        )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None

    try:
        config = load_repo_config(repo_root)
        with FlowStore(db_path, durable=config.durable_writes) as local_store:
            records = local_store.list_flow_runs(flow_type="ticket_flow")
            return select_authoritative_run_record(
                records,
                preferred_run_id=preferred_run_id,
                represented_run_id=represented_run_id,
            )
    except Exception:
        return None


def _resolve_last_event_meta(
    *,
    repo_root: Path,
    record: Optional[FlowRunRecord],
    store: Optional[FlowStore],
    preferred_run_id: Optional[str],
    represented_run_id: Optional[str],
) -> tuple[Optional[FlowRunRecord], Optional[int], Optional[str]]:
    if record is None:
        record = resolve_authoritative_ticket_flow_run(
            repo_root,
            store=store,
            preferred_run_id=preferred_run_id,
            represented_run_id=represented_run_id,
        )

    if record is None:
        return None, None, None

    if store is None:
        return record, None, None

    try:
        seq, event_at = store.get_last_event_meta(str(record.id))
    except Exception:
        return record, None, None
    return (
        record,
        _normalize_optional_int(seq),
        _normalize_optional_str(event_at),
    )


def build_canonical_state_v1(
    *,
    repo_root: Path,
    repo_id: str,
    run_state: Optional[dict[str, Any]],
    record: Optional[FlowRunRecord] = None,
    store: Optional[FlowStore] = None,
    preferred_run_id: Optional[str] = None,
    stale_threshold_seconds: Optional[int] = None,
    census: Optional[TicketFlowCensus] = None,
    run_facts: Optional[AuthoritativeRunFacts] = None,
) -> dict[str, Any]:
    observed_at = _iso_now()

    if census is None:
        census = collect_ticket_flow_census(repo_root)
    frontmatter_total_count = census.total_count
    frontmatter_done_count = census.done_count
    effective_next_ticket = _normalize_optional_str(census.effective_next_ticket)

    run_state_payload = run_state if isinstance(run_state, dict) else {}
    run_state_run_id = _normalize_optional_str(run_state_payload.get("run_id"))
    record_run_id = (
        _normalize_optional_str(getattr(record, "id", None))
        if record is not None
        else None
    )
    represented_run_id = record_run_id or run_state_run_id

    if run_facts is not None:
        latest_record = run_facts.record
        last_event_seq = run_facts.last_event_seq
        last_event_at = run_facts.last_event_at
    else:
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

    freshness = build_freshness_payload(
        generated_at=observed_at,
        stale_threshold_seconds=stale_threshold_seconds,
        candidates=[
            ("last_event_at", last_event_at),
            ("run_state_last_progress_at", run_state_payload.get("last_progress_at")),
            ("latest_run_finished_at", getattr(latest_record, "finished_at", None)),
            ("latest_run_started_at", getattr(latest_record, "started_at", None)),
            ("latest_run_created_at", getattr(latest_record, "created_at", None)),
            ("ticket_ingested_at", ingested_at),
        ],
    )

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
        "freshness": freshness,
    }
