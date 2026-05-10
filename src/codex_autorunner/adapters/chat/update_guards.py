from __future__ import annotations

from typing import Any


def update_thread_blocks_restart_warning(thread: Any) -> bool:
    thread_kind = str(getattr(thread, "thread_kind", "") or "").strip().lower()
    return thread_kind != "ticket_flow"


def active_managed_update_session_count(orchestration_service: Any) -> int:
    try:
        threads = orchestration_service.list_thread_targets(lifecycle_status="active")
    except (AttributeError, TypeError, RuntimeError):
        return 0
    get_running_execution = getattr(
        orchestration_service, "get_running_execution", None
    )
    if not callable(get_running_execution):
        return sum(
            1
            for thread in threads
            if update_thread_blocks_restart_warning(thread)
            if str(getattr(thread, "status", "") or "").strip().lower() == "running"
        )

    active_count = 0
    for thread in threads:
        if not update_thread_blocks_restart_warning(thread):
            continue
        thread_target_id = str(getattr(thread, "thread_target_id", "") or "").strip()
        if not thread_target_id:
            continue
        try:
            if get_running_execution(thread_target_id) is not None:
                active_count += 1
        except (AttributeError, TypeError, RuntimeError):
            if str(getattr(thread, "status", "") or "").strip().lower() == "running":
                active_count += 1
    return active_count
