from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from .freshness import build_freshness_payload, iso_now, resolve_stale_threshold_seconds
from .pma_context_shared import THREAD_SNAPSHOT_FIELDS
from .pma_file_inbox import (
    PMA_FILE_NEXT_ACTION_REVIEW_STALE,
    _extract_entry_freshness,
    _timestamp_sort_value,
    enrich_pma_file_inbox_entry,
)
from .pma_thread_classification import (
    classify_thread_followup,
    is_pma_self_thread,
    thread_followup_state_rank,
)

_logger = logging.getLogger(__name__)

PMA_ACTION_QUEUE_PRECEDENCE: dict[str, tuple[int, str]] = {
    "ticket_flow_inbox": (10, "ticket_flow_inbox"),
    "automation_wakeup": (15, "automation_wakeup"),
    "managed_thread_followup": (20, "managed_thread_followup"),
    "pma_file_inbox": (30, "pma_file_inbox"),
}


def _queue_precedence(source: str) -> tuple[int, str]:
    return PMA_ACTION_QUEUE_PRECEDENCE.get(source, (999, source or "unknown"))


def _queue_supersession_payload(
    *,
    status: str,
    is_primary: bool,
    superseded: bool,
    superseded_by: Optional[str],
    reason: Optional[str],
) -> dict[str, Any]:
    return {
        "status": status,
        "is_primary": is_primary,
        "superseded": superseded,
        "superseded_by": superseded_by,
        "reason": reason,
    }


def _thread_group_owner_label(
    *,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    workspace_root: Optional[str],
) -> str:
    if repo_id:
        return f"repo {repo_id}"
    if resource_kind and resource_id:
        return f"{resource_kind}:{resource_id}"
    if workspace_root:
        return workspace_root
    return "unowned threads"


def _collect_thread_owner_labels(items: Sequence[Mapping[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = _thread_group_owner_label(
            repo_id=str(item.get("repo_id") or "").strip() or None,
            resource_kind=str(item.get("resource_kind") or "").strip() or None,
            resource_id=str(item.get("resource_id") or "").strip() or None,
            workspace_root=str(item.get("workspace_root") or "").strip() or None,
        )
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _build_low_signal_thread_summary_item(
    *,
    followup_state: str,
    items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    newest = max(items, key=lambda item: float(item.get("sort_timestamp") or 0.0))
    owner_labels = _collect_thread_owner_labels(items)
    repo_ids = sorted(
        {
            str(item.get("repo_id") or "").strip()
            for item in items
            if str(item.get("repo_id") or "").strip()
        }
    )
    summary_repo_id = None
    if len(repo_ids) == 1:
        candidate_repo_id = repo_ids[0]
        if all(
            str(item.get("repo_id") or "").strip() == candidate_repo_id
            for item in items
        ):
            summary_repo_id = candidate_repo_id
    if followup_state == "idle_archive_candidate":
        name = f"Dormant cleanup candidates ({len(items)})"
        operator_need = "cleanup"
        why_selected = (
            f"Collapsed {len(items)} dormant cleanup candidates into a counts-first "
            "summary so cleanup inventory does not dominate the main queue"
        )
        recommended_action = "show_cleanup_candidates"
        recommended_detail = (
            "Show cleanup candidates to inspect the dormant managed threads before "
            "archiving any of them."
        )
        drilldown_commands = ["show cleanup candidates"]
    elif followup_state == "protected_chat_bound":
        name = f"Protected chat-bound threads ({len(items)})"
        operator_need = "protected"
        why_selected = (
            f"Collapsed {len(items)} protected chat-bound threads into a counts-first "
            "summary so continuity-bearing chat state stays separate from generic "
            "cleanup inventory"
        )
        recommended_action = "show_protected_threads"
        recommended_detail = (
            "Show protected threads to review chat-bound continuity artifacts. "
            "These are protected by default and should not be archived or removed "
            'unless the user is very explicit; broad requests like "clean up '
            'workspace" are not enough.'
        )
        drilldown_commands = ["show protected threads"]
    else:
        name = f"Reusable managed threads ({len(items)})"
        operator_need = "optional"
        why_selected = (
            f"Collapsed {len(items)} reusable managed threads into a counts-first "
            "summary so optional reuse stays behind true next-action work"
        )
        recommended_action = "show_reusable_threads"
        recommended_detail = (
            "Show reusable threads to inspect the available managed threads before "
            "reusing one."
        )
        drilldown_commands = ["show reusable threads"]

    if owner_labels:
        owner_preview = ", ".join(owner_labels[:3])
        if len(owner_labels) > 3:
            owner_preview += ", ..."
        recommended_detail += f" Owners: {owner_preview}."

    return {
        "item_type": "managed_thread_followup_summary",
        "queue_source": "managed_thread_followup",
        "action_queue_id": f"managed_thread_followup_summary:{followup_state}",
        "precedence": dict(newest.get("precedence") or {}),
        "repo_id": summary_repo_id,
        "name": name,
        "thread_count": len(items),
        "managed_thread_ids": [
            item.get("managed_thread_id")
            for item in items
            if item.get("managed_thread_id")
        ],
        "chat_bound_thread_count": sum(1 for item in items if item.get("chat_bound")),
        "followup_state": followup_state,
        "followup_state_counts": {followup_state: len(items)},
        "operator_need": operator_need,
        "operator_need_rank": thread_followup_state_rank(followup_state),
        "why_selected": why_selected,
        "recommended_action": recommended_action,
        "recommended_detail": recommended_detail,
        "drilldown_commands": drilldown_commands,
        "cleanup_protected": followup_state == "protected_chat_bound",
        "owner_count": len(owner_labels),
        "owner_labels": owner_labels,
        "freshness": (
            dict(newest.get("freshness") or {})
            if isinstance(newest.get("freshness"), Mapping)
            else None
        ),
        "scope": {
            "kind": "thread_summary",
            "key": f"thread_summary:{followup_state}",
        },
        "sort_timestamp": newest.get("sort_timestamp"),
    }


def _collapse_low_signal_thread_queue_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    passthrough: list[dict[str, Any]] = []
    reusable_items: list[dict[str, Any]] = []
    protected_items: list[dict[str, Any]] = []
    cleanup_items: list[dict[str, Any]] = []

    for item in items:
        followup_state = str(item.get("followup_state") or "").strip().lower()
        if followup_state == "reusable":
            reusable_items.append(item)
            continue
        if followup_state == "protected_chat_bound":
            protected_items.append(item)
            continue
        if followup_state == "idle_archive_candidate":
            cleanup_items.append(item)
            continue
        passthrough.append(item)

    collapsed: list[dict[str, Any]] = list(passthrough)
    if reusable_items:
        collapsed.append(
            _build_low_signal_thread_summary_item(
                followup_state="reusable",
                items=reusable_items,
            )
        )
    if protected_items:
        collapsed.append(
            _build_low_signal_thread_summary_item(
                followup_state="protected_chat_bound",
                items=protected_items,
            )
        )
    if cleanup_items:
        collapsed.append(
            _build_low_signal_thread_summary_item(
                followup_state="idle_archive_candidate",
                items=cleanup_items,
            )
        )

    return collapsed


def _build_ticket_flow_queue_items(
    inbox: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("ticket_flow_inbox")
    for entry in inbox:
        if not isinstance(entry, dict):
            continue
        copied = dict(entry)
        repo_id = str(copied.get("repo_id") or "").strip()
        run_id = str(copied.get("run_id") or "").strip()
        seq = copied.get("seq")
        queue_id = f"ticket_flow_inbox:{repo_id or '-'}:{run_id or '-'}:{seq or 0}"
        recommended_detail = (
            str((copied.get("run_state") or {}).get("recommended_action") or "").strip()
            or None
        )
        next_action = (
            str(copied.get("next_action") or "").strip() or "inspect_and_resume"
        )
        freshness = _extract_entry_freshness(copied)
        copied.update(
            {
                "action_queue_id": queue_id,
                "queue_source": "ticket_flow_inbox",
                "precedence": {"rank": rank, "label": label},
                "why_selected": (
                    "Newest authoritative ticket-flow run requires operator attention"
                    if copied.get("dispatch_actionable") is False
                    else "Newest authoritative ticket-flow run has an unanswered dispatch"
                ),
                "recommended_action": next_action,
                "recommended_detail": recommended_detail,
                "freshness": (
                    dict(freshness) if isinstance(freshness, Mapping) else None
                ),
                "scope": {
                    "kind": "run",
                    "key": f"run:{run_id}" if run_id else f"repo:{repo_id}",
                },
                "sort_timestamp": _timestamp_sort_value(
                    (freshness or {}).get("basis_at")
                    if isinstance(freshness, Mapping)
                    else copied.get("run_created_at")
                ),
            }
        )
        items.append(copied)
    return items


def _build_thread_queue_items(
    pma_threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("managed_thread_followup")
    for entry in pma_threads:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status in {"", "archived"}:
            continue
        freshness = _extract_entry_freshness(entry)
        is_stale = bool(
            isinstance(freshness, Mapping) and freshness.get("is_stale") is True
        )
        if status == "running":
            if is_stale and is_pma_self_thread(entry):
                continue
            if not is_stale:
                continue
        repo_id = str(entry.get("repo_id") or "").strip()
        managed_thread_id = str(entry.get("managed_thread_id") or "").strip()
        is_chat_bound = bool(
            entry.get("chat_bound") is True or entry.get("cleanup_protected") is True
        )
        semantics = classify_thread_followup(
            entry,
            is_stale=is_stale,
            is_chat_bound=is_chat_bound,
            is_self_thread=is_pma_self_thread(entry),
        )
        next_action = str(semantics.get("recommended_action") or "").strip() or None
        why_selected = str(semantics.get("why_selected") or "").strip() or None
        detail_template = (
            str(semantics.get("recommended_detail_template") or "").strip() or None
        )
        scope_key = (
            f"thread:{managed_thread_id}"
            if managed_thread_id
            else (
                f"resource:{entry.get('resource_kind')}:{entry.get('resource_id')}"
                if entry.get("resource_kind") and entry.get("resource_id")
                else f"repo:{repo_id}"
            )
        )
        item: dict[str, Any] = {
            k: entry.get(k) for k in THREAD_SNAPSHOT_FIELDS if k in entry
        }
        item["managed_thread_id"] = managed_thread_id or None
        item["repo_id"] = repo_id or None
        item["chat_bound"] = bool(entry.get("chat_bound"))
        item["cleanup_protected"] = bool(entry.get("cleanup_protected"))
        item["binding_count"] = int(entry.get("binding_count") or 0)
        item["binding_kinds"] = list(entry.get("binding_kinds") or [])
        item["binding_ids"] = list(entry.get("binding_ids") or [])
        item.update(
            {
                "item_type": "managed_thread_followup",
                "open_url": (
                    f"/hub/pma/threads/{managed_thread_id}"
                    if managed_thread_id
                    else None
                ),
                "action_queue_id": f"managed_thread_followup:{managed_thread_id or '-'}",
                "queue_source": "managed_thread_followup",
                "precedence": {"rank": rank, "label": label},
                "followup_state": semantics.get("followup_state"),
                "operator_need": semantics.get("operator_need"),
                "operator_need_rank": thread_followup_state_rank(
                    str(semantics.get("followup_state") or "")
                ),
                "why_selected": why_selected,
                "recommended_action": next_action,
                "recommended_detail": (
                    detail_template.format(managed_thread_id=managed_thread_id)
                    if managed_thread_id and detail_template
                    else None
                ),
                "freshness": (
                    dict(freshness) if isinstance(freshness, Mapping) else None
                ),
                "scope": {
                    "kind": "thread" if managed_thread_id else "resource",
                    "key": scope_key,
                },
                "sort_timestamp": _timestamp_sort_value(
                    (freshness or {}).get("basis_at")
                    if isinstance(freshness, Mapping)
                    else entry.get("updated_at")
                ),
            }
        )
        items.append(item)
    return _collapse_low_signal_thread_queue_items(items)


def _build_file_queue_items(
    pma_files_detail: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    stale_items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("pma_file_inbox")
    for entry in pma_files_detail.get("inbox") or []:
        if not isinstance(entry, dict):
            continue
        copied = enrich_pma_file_inbox_entry(entry)
        freshness = _extract_entry_freshness(copied)
        name = str(copied.get("name") or "").strip()
        copied.update(
            {
                "action_queue_id": f"pma_file_inbox:{name or '-'}",
                "queue_source": "pma_file_inbox",
                "precedence": {"rank": rank, "label": label},
                "freshness": (
                    dict(freshness) if isinstance(freshness, Mapping) else None
                ),
                "scope": {"kind": "filebox", "key": f"filebox:inbox:{name or '-'}"},
                "sort_timestamp": _timestamp_sort_value(
                    (freshness or {}).get("basis_at")
                    if isinstance(freshness, Mapping)
                    else entry.get("modified_at")
                ),
            }
        )
        if copied.get("next_action") == PMA_FILE_NEXT_ACTION_REVIEW_STALE:
            stale_items.append(copied)
            continue
        items.append(copied)
    if stale_items:
        newest = max(
            stale_items, key=lambda item: float(item.get("sort_timestamp") or 0.0)
        )
        items.append(
            {
                "item_type": "pma_file_summary",
                "queue_source": "pma_file_inbox",
                "action_queue_id": "pma_file_inbox_summary:stale_uploaded_files",
                "precedence": {"rank": rank, "label": label},
                "name": f"Stale uploaded files ({len(stale_items)})",
                "file_count": len(stale_items),
                "file_names": [
                    str(item.get("name") or "").strip()
                    for item in stale_items
                    if str(item.get("name") or "").strip()
                ],
                "next_action": PMA_FILE_NEXT_ACTION_REVIEW_STALE,
                "recommended_action": "show_stale_uploaded_files",
                "recommended_detail": (
                    "Show stale uploaded files to inspect likely leftovers before "
                    "deleting or routing any of them."
                ),
                "why_selected": (
                    f"Collapsed {len(stale_items)} stale uploaded files into a "
                    "counts-first summary so likely leftovers do not dominate the "
                    "main queue"
                ),
                "operator_need": "review",
                "operator_need_rank": 40,
                "likely_false_positive": True,
                "drilldown_commands": ["show stale uploaded files"],
                "freshness": (
                    dict(newest.get("freshness") or {})
                    if isinstance(newest.get("freshness"), Mapping)
                    else None
                ),
                "scope": {
                    "kind": "filebox_summary",
                    "key": "filebox_summary:stale_uploaded_files",
                },
                "sort_timestamp": newest.get("sort_timestamp"),
            }
        )
    return items


def _build_automation_queue_items(
    automation: Mapping[str, Any],
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rank, label = _queue_precedence("automation_wakeup")
    wakeups = (automation.get("wakeups") or {}).get("pending_sample") or []
    for entry in wakeups:
        if not isinstance(entry, dict):
            continue
        repo_id = str(entry.get("repo_id") or "").strip()
        thread_id = str(entry.get("thread_id") or "").strip()
        wakeup_id = str(entry.get("wakeup_id") or "").strip()
        basis_at = entry.get("timestamp")
        freshness = build_freshness_payload(
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
            candidates=[("automation_wakeup", basis_at)],
        )
        scope_key = (
            f"thread:{thread_id}"
            if thread_id
            else (
                f"run:{entry.get('run_id')}"
                if entry.get("run_id")
                else (f"repo:{repo_id}" if repo_id else f"wakeup:{wakeup_id}")
            )
        )
        items.append(
            {
                "item_type": "automation_wakeup",
                "wakeup_id": wakeup_id or None,
                "source": entry.get("source"),
                "event_type": entry.get("event_type"),
                "subscription_id": entry.get("subscription_id"),
                "timer_id": entry.get("timer_id"),
                "repo_id": repo_id or None,
                "run_id": entry.get("run_id"),
                "thread_id": thread_id or None,
                "lane_id": entry.get("lane_id"),
                "from_state": entry.get("from_state"),
                "to_state": entry.get("to_state"),
                "reason": entry.get("reason"),
                "timestamp": basis_at,
                "action_queue_id": f"automation_wakeup:{wakeup_id or scope_key}",
                "queue_source": "automation_wakeup",
                "precedence": {"rank": rank, "label": label},
                "why_selected": "Pending automation wakeup signals follow-up work",
                "recommended_action": "handle_automation_wakeup",
                "recommended_detail": (
                    f"Continue lane {entry.get('lane_id')}"
                    if entry.get("lane_id")
                    else "Inspect the pending PMA automation wakeup"
                ),
                "operator_need_rank": 5,
                "freshness": freshness,
                "scope": {
                    "kind": "wakeup",
                    "key": scope_key,
                },
                "sort_timestamp": _timestamp_sort_value(basis_at),
            }
        )
    return items


def _is_strong_action_queue_item(item: Mapping[str, Any]) -> bool:
    if bool(item.get("likely_false_positive")):
        return False

    item_type = str(item.get("item_type") or "").strip().lower()
    followup_state = str(item.get("followup_state") or "").strip().lower()
    operator_need = str(item.get("operator_need") or "").strip().lower()

    if item_type in {
        "managed_thread_followup_summary",
        "pma_file_summary",
    }:
        return False
    if item_type == "managed_thread_followup":
        return followup_state in {"attention_required", "awaiting_followup"}
    if item_type == "pma_file":
        return True
    if item_type == "automation_wakeup":
        return True
    if item.get("queue_source") == "ticket_flow_inbox":
        return True
    return operator_need in {"urgent", "normal"}


def build_pma_action_queue(
    *,
    inbox: list[dict[str, Any]],
    pma_threads: list[dict[str, Any]],
    pma_files_detail: Mapping[str, list[dict[str, Any]]],
    automation: Mapping[str, Any],
    generated_at: Optional[str] = None,
    stale_threshold_seconds: Optional[int] = None,
) -> list[dict[str, Any]]:
    resolved_generated_at = generated_at or iso_now()
    resolved_stale_threshold = resolve_stale_threshold_seconds(stale_threshold_seconds)
    items = [
        *_build_ticket_flow_queue_items(inbox),
        *_build_thread_queue_items(pma_threads),
        *_build_file_queue_items(pma_files_detail),
        *_build_automation_queue_items(
            automation,
            generated_at=resolved_generated_at,
            stale_threshold_seconds=resolved_stale_threshold,
        ),
    ]

    items = sorted(
        items,
        key=lambda item: (
            int(((item.get("precedence") or {}).get("rank") or 999)),
            int(item.get("operator_need_rank") or 0),
            -float(item.get("sort_timestamp") or 0.0),
            str(item.get("action_queue_id") or ""),
        ),
    )

    winning_scope: dict[str, dict[str, Any]] = {}
    winning_repo_blocker: dict[str, dict[str, Any]] = {}
    primary_queue_id = next(
        (
            str(item.get("action_queue_id") or "")
            for item in items
            if _is_strong_action_queue_item(item)
        ),
        None,
    )
    for index, item in enumerate(items, start=1):
        scope = item.get("scope") or {}
        scope_key = str(scope.get("key") or "")
        queue_id = str(item.get("action_queue_id") or "")
        repo_key = str(item.get("repo_id") or "").strip()
        repo_blocker = winning_repo_blocker.get(repo_key) if repo_key else None
        scope_winner = winning_scope.get(scope_key) if scope_key else None
        winner = repo_blocker or scope_winner
        if winner is not None:
            item["supersession"] = _queue_supersession_payload(
                status="superseded",
                is_primary=False,
                superseded=True,
                superseded_by=str(winner.get("action_queue_id") or "") or None,
                reason=(
                    "A higher-precedence action already covers the same operator scope"
                ),
            )
        else:
            if scope_key:
                winning_scope[scope_key] = item
            is_primary = bool(primary_queue_id and queue_id == primary_queue_id)
            item["supersession"] = _queue_supersession_payload(
                status="primary" if is_primary else "non_primary",
                is_primary=is_primary,
                superseded=False,
                superseded_by=None,
                reason=(
                    "Highest-priority actionable item in the queue"
                    if is_primary
                    else (
                        "Actionable, but lower priority than the current primary item"
                        if primary_queue_id
                        else "Inventory or hygiene item; no strong next action currently"
                    )
                ),
            )
        if (
            str(item.get("queue_source") or "") == "ticket_flow_inbox"
            and repo_key
            and repo_key not in winning_repo_blocker
        ):
            winning_repo_blocker[repo_key] = item
        item["queue_rank"] = index
        item.pop("sort_timestamp", None)
    return items
