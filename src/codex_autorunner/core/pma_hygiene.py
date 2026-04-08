from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .config import load_hub_config
from .filebox import delete_file, list_filebox
from .freshness import build_freshness_payload, iso_now, resolve_stale_threshold_seconds
from .orchestration.bindings import OrchestrationBindingStore
from .pma_automation_store import PmaAutomationStore
from .pma_dispatches import list_pma_dispatches
from .pma_thread_store import PmaThreadStore

PMA_HYGIENE_CATEGORY_ALIASES = {
    "all": ("files", "threads", "automation", "alerts"),
    "alerts": ("alerts",),
    "dispatches": ("alerts",),
    "files": ("files",),
    "threads": ("threads",),
    "automation": ("automation",),
}
PMA_HYGIENE_GROUP_ORDER = ("safe", "protected", "needs-confirmation")
PMA_HYGIENE_CATEGORY_ORDER = ("files", "threads", "automation", "alerts")
PMA_HYGIENE_LOCK_NAME = "pma_hygiene.lock"


def _hygiene_lock_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / PMA_HYGIENE_LOCK_NAME


def _canonicalize_categories(categories: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        category for category in PMA_HYGIENE_CATEGORY_ORDER if category in categories
    )


def _normalize_categories(raw: Optional[Sequence[str]]) -> tuple[str, ...]:
    if not raw:
        return PMA_HYGIENE_CATEGORY_ALIASES["all"]
    normalized: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            key = part.strip().lower()
            if not key:
                continue
            expanded = PMA_HYGIENE_CATEGORY_ALIASES.get(key)
            if expanded is None:
                raise ValueError(
                    "Unknown PMA hygiene category "
                    f"{part!r}. Expected one of: alerts, automation, files, threads."
                )
            for item in expanded:
                if item not in normalized:
                    normalized.append(item)
    return _canonicalize_categories(normalized or PMA_HYGIENE_CATEGORY_ALIASES["all"])


def _load_stale_threshold_seconds(
    hub_root: Path, override: Optional[int] = None
) -> int:
    if isinstance(override, int) and override > 0:
        return override
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        return resolve_stale_threshold_seconds(
            getattr(pma_cfg, "freshness_stale_threshold_seconds", None)
        )
    except (OSError, ValueError):
        return resolve_stale_threshold_seconds(None)


def _group_sort_key(group: str) -> int:
    try:
        return PMA_HYGIENE_GROUP_ORDER.index(group)
    except ValueError:
        return len(PMA_HYGIENE_GROUP_ORDER)


def _category_sort_key(category: str) -> int:
    try:
        return PMA_HYGIENE_CATEGORY_ORDER.index(category)
    except ValueError:
        return len(PMA_HYGIENE_CATEGORY_ORDER)


def _hygiene_item_sort_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
    return (
        _group_sort_key(str(item.get("group") or "")),
        _category_sort_key(str(item.get("category") or "")),
        str(item.get("label") or ""),
        str(item.get("candidate_id") or ""),
    )


def _build_candidate(
    *,
    group: str,
    category: str,
    candidate_id: str,
    label: str,
    action: str,
    reason: str,
    evidence: dict[str, Any],
    path: Optional[str] = None,
    target: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "group": group,
        "category": category,
        "label": label,
        "action": action,
        "reason": reason,
        "path": path,
        "evidence": evidence,
        "target": target or {},
        "apply_allowed": group == "safe",
    }


def _build_file_candidates(
    hub_root: Path,
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> list[dict[str, Any]]:
    listing = list_filebox(hub_root)
    candidates: list[dict[str, Any]] = []
    for entry in listing.get("inbox") or []:
        freshness = build_freshness_payload(
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
            candidates=[("file_modified_at", entry.modified_at)],
        )
        if freshness.get("is_stale") is not True:
            continue
        candidates.append(
            _build_candidate(
                group="needs-confirmation",
                category="files",
                candidate_id=f"files:inbox:{entry.name}",
                label=f"inbox/{entry.name}",
                action="review_stale_uploaded_file",
                reason=("Stale PMA inbox upload requires review before deletion."),
                evidence={
                    "modified_at": entry.modified_at,
                    "source": entry.source,
                    "size": entry.size,
                    "freshness": freshness,
                    "next_action": "review_stale_uploaded_file",
                },
                path=str(entry.path),
                target={"box": "inbox", "filename": entry.name},
            )
        )
    return candidates


def _build_thread_candidates(
    hub_root: Path,
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> list[dict[str, Any]]:
    try:
        store = PmaThreadStore(hub_root)
        threads = store.list_threads(limit=500)
        busy_ids = set(store.list_thread_ids_with_running_executions(limit=None))
        busy_ids.update(store.list_thread_ids_with_pending_queue(limit=None))
        binding_store = OrchestrationBindingStore(hub_root)
    except (
        Exception
    ):  # intentional: multi-store init + DB queries; safe fallback to empty
        return []

    candidates: list[dict[str, Any]] = []
    for thread in threads:
        managed_thread_id = str(thread.get("managed_thread_id") or "").strip()
        if not managed_thread_id:
            continue
        lifecycle_status = str(
            thread.get("lifecycle_status") or thread.get("status") or ""
        )
        normalized_status = str(
            thread.get("normalized_status") or thread.get("status") or ""
        ).strip()
        if lifecycle_status == "archived" or normalized_status == "archived":
            continue
        freshness = build_freshness_payload(
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
            candidates=[("thread_updated_at", thread.get("updated_at"))],
        )
        if freshness.get("is_stale") is not True:
            continue
        try:
            bindings = binding_store.list_bindings(
                thread_target_id=managed_thread_id,
                include_disabled=False,
                limit=50,
            )
        except (OSError, ValueError):
            bindings = []
        surface_kinds = sorted(
            {
                str(binding.surface_kind).strip()
                for binding in bindings
                if str(binding.surface_kind).strip()
            }
        )
        has_binding = bool(bindings)
        has_busy_work = managed_thread_id in busy_ids
        group = "protected" if (has_binding or has_busy_work) else "needs-confirmation"
        reason = (
            "Managed thread still has an active binding or work in flight."
            if group == "protected"
            else "Idle managed-thread followup is dormant but may still be reusable."
        )
        candidates.append(
            _build_candidate(
                group=group,
                category="threads",
                candidate_id=f"threads:{managed_thread_id}",
                label=managed_thread_id,
                action="archive_managed_thread",
                reason=reason,
                evidence={
                    "status": normalized_status or lifecycle_status or None,
                    "updated_at": thread.get("updated_at"),
                    "binding_count": len(bindings),
                    "surface_kinds": surface_kinds,
                    "has_busy_work": has_busy_work,
                    "freshness": freshness,
                    "repo_id": thread.get("repo_id"),
                    "resource_kind": thread.get("resource_kind"),
                    "resource_id": thread.get("resource_id"),
                },
                target={"managed_thread_id": managed_thread_id},
            )
        )
    return candidates


def _build_automation_candidates(hub_root: Path) -> list[dict[str, Any]]:
    try:
        store = PmaAutomationStore(hub_root)
    except (OSError, ValueError):
        return []

    candidates: list[dict[str, Any]] = []

    for entry in store.list_subscriptions(include_inactive=True):
        subscription_id = str(entry.get("subscription_id") or "").strip()
        if not subscription_id:
            continue
        state = str(entry.get("state") or "active").strip().lower()
        group = "protected" if state == "active" else "safe"
        reason = (
            "Active automation subscription is still watching for transitions."
            if group == "protected"
            else "Inactive automation subscription is safe to purge."
        )
        candidates.append(
            _build_candidate(
                group=group,
                category="automation",
                candidate_id=f"automation:subscription:{subscription_id}",
                label=f"subscription/{subscription_id}",
                action="purge_subscription",
                reason=reason,
                evidence={
                    "state": state,
                    "event_types": entry.get("event_types"),
                    "updated_at": entry.get("updated_at"),
                    "repo_id": entry.get("repo_id"),
                    "run_id": entry.get("run_id"),
                    "thread_id": entry.get("thread_id"),
                    "lane_id": entry.get("lane_id"),
                    "match_count": entry.get("match_count"),
                    "max_matches": entry.get("max_matches"),
                },
                target={"subscription_id": subscription_id},
            )
        )

    for entry in store.list_timers(include_inactive=True):
        timer_id = str(entry.get("timer_id") or "").strip()
        if not timer_id:
            continue
        state = str(entry.get("state") or "pending").strip().lower()
        group = "protected" if state == "pending" else "safe"
        reason = (
            "Pending automation timer may still fire and should not be removed implicitly."
            if group == "protected"
            else "Inactive automation timer is safe to purge."
        )
        candidates.append(
            _build_candidate(
                group=group,
                category="automation",
                candidate_id=f"automation:timer:{timer_id}",
                label=f"timer/{timer_id}",
                action="purge_timer",
                reason=reason,
                evidence={
                    "state": state,
                    "timer_type": entry.get("timer_type"),
                    "due_at": entry.get("due_at"),
                    "updated_at": entry.get("updated_at"),
                    "repo_id": entry.get("repo_id"),
                    "run_id": entry.get("run_id"),
                    "thread_id": entry.get("thread_id"),
                    "lane_id": entry.get("lane_id"),
                },
                target={"timer_id": timer_id},
            )
        )

    for entry in store.list_wakeups():
        wakeup_id = str(entry.get("wakeup_id") or "").strip()
        if not wakeup_id:
            continue
        state = str(entry.get("state") or "pending").strip().lower()
        group = "protected" if state == "pending" else "safe"
        reason = (
            "Pending automation wakeup still represents queued follow-up work."
            if group == "protected"
            else "Delivered automation wakeup is safe to purge."
        )
        candidates.append(
            _build_candidate(
                group=group,
                category="automation",
                candidate_id=f"automation:wakeup:{wakeup_id}",
                label=f"wakeup/{wakeup_id}",
                action="purge_wakeup",
                reason=reason,
                evidence={
                    "state": state,
                    "source": entry.get("source"),
                    "event_type": entry.get("event_type"),
                    "timestamp": entry.get("timestamp"),
                    "updated_at": entry.get("updated_at"),
                    "repo_id": entry.get("repo_id"),
                    "run_id": entry.get("run_id"),
                    "thread_id": entry.get("thread_id"),
                    "lane_id": entry.get("lane_id"),
                },
                target={"wakeup_id": wakeup_id},
            )
        )
    return candidates


def _build_alert_candidates(
    hub_root: Path,
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for dispatch in list_pma_dispatches(hub_root, include_resolved=True, limit=500):
        resolved_at = dispatch.resolved_at
        freshness = build_freshness_payload(
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
            candidates=[
                ("dispatch_resolved_at", resolved_at),
                ("dispatch_created_at", dispatch.created_at),
            ],
        )
        group = "safe" if resolved_at else "protected"
        reason = (
            "Resolved PMA dispatch alert is safe to delete."
            if group == "safe"
            else "Unresolved PMA dispatch still needs operator visibility."
        )
        candidates.append(
            _build_candidate(
                group=group,
                category="alerts",
                candidate_id=f"alerts:{dispatch.dispatch_id}",
                label=dispatch.title or dispatch.dispatch_id,
                action="delete_pma_dispatch",
                reason=reason,
                evidence={
                    "priority": dispatch.priority,
                    "created_at": dispatch.created_at,
                    "resolved_at": resolved_at,
                    "freshness": freshness,
                    "source_turn_id": dispatch.source_turn_id,
                },
                path=str(dispatch.path),
                target={
                    "dispatch_path": str(dispatch.path),
                    "dispatch_id": dispatch.dispatch_id,
                },
            )
        )
    return candidates


def _iter_report_candidates(
    hub_root: Path,
    *,
    categories: Sequence[str],
    generated_at: str,
    stale_threshold_seconds: int,
) -> Iterable[dict[str, Any]]:
    if "files" in categories:
        yield from _build_file_candidates(
            hub_root,
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
        )
    if "threads" in categories:
        yield from _build_thread_candidates(
            hub_root,
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
        )
    if "automation" in categories:
        yield from _build_automation_candidates(hub_root)
    if "alerts" in categories:
        yield from _build_alert_candidates(
            hub_root,
            generated_at=generated_at,
            stale_threshold_seconds=stale_threshold_seconds,
        )


def build_pma_hygiene_report(
    hub_root: Path,
    *,
    categories: Optional[Sequence[str]] = None,
    stale_threshold_seconds: Optional[int] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    resolved_categories = _normalize_categories(categories)
    generated_at_text = generated_at or iso_now()
    threshold_seconds = _load_stale_threshold_seconds(
        hub_root, override=stale_threshold_seconds
    )
    items = list(
        _iter_report_candidates(
            hub_root,
            categories=resolved_categories,
            generated_at=generated_at_text,
            stale_threshold_seconds=threshold_seconds,
        )
    )
    items.sort(key=_hygiene_item_sort_key)

    groups: dict[str, list[dict[str, Any]]] = {
        group: [] for group in PMA_HYGIENE_GROUP_ORDER
    }
    for item in items:
        groups.setdefault(str(item.get("group") or "needs-confirmation"), []).append(
            item
        )

    group_counts = {
        group: len(groups.get(group, [])) for group in PMA_HYGIENE_GROUP_ORDER
    }
    category_counts = Counter(str(item.get("category") or "") for item in items)

    return {
        "generated_at": generated_at_text,
        "stale_threshold_seconds": threshold_seconds,
        "categories": list(resolved_categories),
        "groups": groups,
        "summary": {
            "total_candidates": len(items),
            "group_counts": group_counts,
            "category_counts": {
                category: category_counts.get(category, 0)
                for category in PMA_HYGIENE_CATEGORY_ORDER
                if category in resolved_categories
            },
            "safe_apply_count": len(groups.get("safe", [])),
        },
    }


def _is_reviewed_thread_cleanup_candidate(item: dict[str, Any]) -> bool:
    return (
        str(item.get("group") or "") == "needs-confirmation"
        and str(item.get("category") or "") == "threads"
        and str(item.get("action") or "") == "archive_managed_thread"
    )


def _collect_hygiene_apply_items(
    report: dict[str, Any],
    *,
    include_needs_confirmation: bool,
) -> list[dict[str, Any]]:
    raw_groups = report.get("groups")
    groups = raw_groups if isinstance(raw_groups, dict) else {}
    raw_safe_items = groups.get("safe")
    safe_items = (
        [item for item in raw_safe_items if isinstance(item, dict)]
        if isinstance(raw_safe_items, list)
        else []
    )
    reviewed_items: list[dict[str, Any]] = []
    if include_needs_confirmation:
        raw_reviewed_items = groups.get("needs-confirmation")
        reviewed_items = (
            [
                item
                for item in raw_reviewed_items
                if isinstance(item, dict)
                and _is_reviewed_thread_cleanup_candidate(item)
            ]
            if isinstance(raw_reviewed_items, list)
            else []
        )
    selected_items = [*safe_items, *reviewed_items]
    selected_items.sort(key=_hygiene_item_sort_key)
    return selected_items


def apply_pma_hygiene_report(
    hub_root: Path,
    report: dict[str, Any],
    *,
    include_needs_confirmation: bool = False,
) -> dict[str, Any]:
    selected_items = _collect_hygiene_apply_items(
        report, include_needs_confirmation=include_needs_confirmation
    )
    automation_store: Optional[PmaAutomationStore] = None
    thread_store: Optional[PmaThreadStore] = None
    results: list[dict[str, Any]] = []
    safe_attempted = 0
    reviewed_attempted = 0

    for item in selected_items:
        group = str(item.get("group") or "")
        if group == "safe":
            safe_attempted += 1
        elif group == "needs-confirmation":
            reviewed_attempted += 1
        action = str(item.get("action") or "")
        raw_target = item.get("target")
        target: dict[str, Any] = (
            dict(raw_target) if isinstance(raw_target, dict) else {}
        )
        candidate_id = str(item.get("candidate_id") or "")
        ok = False
        error: Optional[str] = None
        try:
            if action == "delete_filebox_file":
                ok = delete_file(
                    hub_root,
                    str(target.get("box") or ""),
                    str(target.get("filename") or ""),
                )
            elif action == "purge_subscription":
                automation_store = automation_store or PmaAutomationStore(hub_root)
                ok = automation_store.purge_subscription(
                    str(target.get("subscription_id") or ""),
                    require_inactive=True,
                )
            elif action == "purge_timer":
                automation_store = automation_store or PmaAutomationStore(hub_root)
                ok = automation_store.purge_timer(
                    str(target.get("timer_id") or ""),
                    require_inactive=True,
                )
            elif action == "purge_wakeup":
                automation_store = automation_store or PmaAutomationStore(hub_root)
                ok = automation_store.purge_wakeup(
                    str(target.get("wakeup_id") or ""),
                    require_inactive=True,
                )
            elif action == "delete_pma_dispatch":
                dispatch_path = Path(str(target.get("dispatch_path") or ""))
                if dispatch_path.is_file():
                    dispatch_path.unlink()
                    ok = True
            elif action == "archive_managed_thread":
                thread_store = thread_store or PmaThreadStore(hub_root)
                thread_store.archive_thread(str(target.get("managed_thread_id") or ""))
                ok = True
        except (
            Exception
        ) as exc:  # intentional: multi-action apply; records per-item failure
            error = str(exc)
        results.append(
            {
                "candidate_id": candidate_id,
                "group": group,
                "category": item.get("category"),
                "action": action,
                "label": item.get("label"),
                "status": "applied" if ok else "failed",
                "error": error,
            }
        )

    return {
        "attempted": len(selected_items),
        "safe_attempted": safe_attempted,
        "reviewed_attempted": reviewed_attempted,
        "applied": sum(1 for item in results if item.get("status") == "applied"),
        "failed": sum(1 for item in results if item.get("status") != "applied"),
        "results": results,
    }


def render_pma_hygiene_report(
    report: dict[str, Any], *, apply: bool = False, summary_only: bool = False
) -> str:
    lines: list[str] = []
    raw_categories = report.get("categories")
    categories = (
        ", ".join(str(category) for category in raw_categories)
        if isinstance(raw_categories, list)
        else ""
    )
    raw_summary = report.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    raw_group_counts = summary.get("group_counts")
    group_counts = raw_group_counts if isinstance(raw_group_counts, dict) else {}
    raw_category_counts = summary.get("category_counts")
    category_counts = (
        raw_category_counts if isinstance(raw_category_counts, dict) else {}
    )
    raw_groups = report.get("groups")
    groups = raw_groups if isinstance(raw_groups, dict) else {}
    mode = "Apply safe cleanup" if apply else "Dry run only"
    lines.append(f"{mode}: PMA hygiene categories={categories}")
    lines.append(
        "Summary: "
        + ", ".join(
            f"{group}={group_counts.get(group, 0)}" for group in PMA_HYGIENE_GROUP_ORDER
        )
    )
    if summary_only:
        if category_counts:
            lines.append(
                "Category counts: "
                + ", ".join(
                    f"{category}={category_counts.get(category, 0)}"
                    for category in PMA_HYGIENE_CATEGORY_ORDER
                    if category in category_counts
                )
            )
        safe_apply_count = summary.get("safe_apply_count")
        if safe_apply_count is not None:
            lines.append(f"Safe apply count: {safe_apply_count}")
        return "\n".join(lines)
    for group in PMA_HYGIENE_GROUP_ORDER:
        raw_items = groups.get(group)
        items = raw_items if isinstance(raw_items, list) else []
        lines.append(f"{group} ({len(items)})")
        for item in items:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "")
            label = str(item.get("label") or "")
            action = str(item.get("action") or "")
            reason = str(item.get("reason") or "")
            raw_evidence = item.get("evidence")
            evidence: dict[str, Any] = (
                dict(raw_evidence) if isinstance(raw_evidence, dict) else {}
            )
            raw_freshness = evidence.get("freshness")
            freshness = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}
            age_seconds = (
                freshness.get("age_seconds") if isinstance(freshness, dict) else None
            )
            age_text = f" age={age_seconds}s" if isinstance(age_seconds, int) else ""
            lines.append(f"- [{category}] {label} -> {action}{age_text}")
            lines.append(f"  reason: {reason}")
    return "\n".join(lines)


__all__ = [
    "PMA_HYGIENE_CATEGORY_ALIASES",
    "apply_pma_hygiene_report",
    "build_pma_hygiene_report",
    "render_pma_hygiene_report",
]
